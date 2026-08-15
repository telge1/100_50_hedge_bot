"""Move-measurement audit: episode fragmentation vs calculation bugs (analysis only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.trend_direction_at import _iso_z, normalize_symbol, run_c34b_on_ohlcv
from research.regime_scanner.trend_direction_forward_validation import (
    BAR_MINUTES,
    _candles_to_ohlcv,
    build_direction_series,
    extract_direction_signals,
    first_touch,
    mfe_mae_pct,
)

FIXED_HORIZONS = (15, 30, 60, 120, 240, 480)
THRESHOLDS = (0.0025, 0.005, 0.01)
MANUAL_APT_CASES = (
    ("2026-04-11T00:55:00Z", "BEARISH"),
    ("2026-04-11T16:10:00Z", "BULLISH"),
    ("2026-04-11T18:10:00Z", "BEARISH"),
    ("2026-04-11T18:45:00Z", "BULLISH"),
    ("2026-04-11T20:45:00Z", "BEARISH"),
)

CODE_PATH_INVENTORY = {
    "signal_extraction": "trend_direction_forward_validation.extract_direction_signals",
    "entry_price": "open of bar signal_index+1 (next_open after confirm close)",
    "episode_begin": "confirming bar close (signal known); forward from next open",
    "episode_end_raw": "first subsequent bar with direction != signal_dir (UNCLEAR or opposite)",
    "forward_horizons": "fixed bars from entry, independent of scanner state",
    "mfe_bullish": "(max_high/entry)-1 then *100 -> percent points",
    "mae_bullish": "1-(min_low/entry) then *100 (positive adverse)",
    "mfe_bearish": "1-(min_low/entry) then *100",
    "mae_bearish": "(max_high/entry)-1 then *100",
    "first_touch": "first_touch() high/low vs targets; SAME_CANDLE_AMBIGUOUS if both same bar",
    "percent_scaling": "percent points (0.27 means 0.27%); single *100 after ratio",
}


def mfe_mae_from_hl(direction: str, entry: float, hi: float, lo: float) -> tuple[float, float]:
    if direction == "BULLISH":
        return ((hi / entry) - 1.0) * 100.0, (1.0 - (lo / entry)) * 100.0
    return (1.0 - (lo / entry)) * 100.0, ((hi / entry) - 1.0) * 100.0


def raw_episode_end(dirs: list[str], signal_i: int, signal_dir: str) -> int:
    for j in range(signal_i + 1, len(dirs)):
        if dirs[j] != signal_dir:
            return j
    return len(dirs)


def until_opposite_end(dirs: list[str], signal_i: int, signal_dir: str) -> int:
    opposite = "BEARISH" if signal_dir == "BULLISH" else "BULLISH"
    for j in range(signal_i + 1, len(dirs)):
        if dirs[j] == opposite:
            return j
    return len(dirs)


def cluster_end_bridge_unclear(
    dirs: list[str], signal_i: int, signal_dir: str, *, max_unclear_minutes: int
) -> int:
    max_bars = max_unclear_minutes // BAR_MINUTES
    n = len(dirs)
    i = signal_i
    while True:
        j = i + 1
        while j < n and dirs[j] == signal_dir:
            j += 1
        if j >= n:
            return n
        if dirs[j] != "UNCLEAR":
            return j
        k = j
        while k < n and dirs[k] == "UNCLEAR":
            k += 1
        if (k - j) > max_bars:
            return j
        if k >= n:
            return n
        if dirs[k] == signal_dir:
            i = k
            continue
        return k


def forward_window(series: pd.DataFrame, signal_i: int, end_excl: int):
    n = len(series)
    if signal_i + 1 >= n:
        return None, np.array([]), np.array([]), 0
    entry = float(series.iloc[signal_i + 1]["open"])
    start = signal_i + 1
    end = min(end_excl, n)
    if end <= start:
        return entry, np.array([]), np.array([]), 0
    sl = series.iloc[start:end]
    return entry, sl["high"].to_numpy(dtype=float), sl["low"].to_numpy(dtype=float), end - start


def measure_window(direction: str, entry, highs, lows) -> dict[str, Any]:
    if entry is None or len(highs) == 0:
        return {"bars": 0, "high": None, "low": None, "mfe_pct": None, "mae_pct": None}
    hi = float(np.max(highs))
    lo = float(np.min(lows))
    mfe, mae = mfe_mae_from_hl(direction, entry, hi, lo)
    return {"bars": int(len(highs)), "high": hi, "low": lo, "mfe_pct": mfe, "mae_pct": mae}


def classify_unclear_transitions(series: pd.DataFrame) -> pd.DataFrame:
    dirs = series["direction"].tolist()
    rows = []
    i = 0
    n = len(dirs)
    while i < n:
        if dirs[i] != "UNCLEAR":
            i += 1
            continue
        start = i
        while i < n and dirs[i] == "UNCLEAR":
            i += 1
        prev = dirs[start - 1] if start > 0 else "NONE"
        nxt = dirs[i] if i < n else "NONE"
        dur = (i - start) * BAR_MINUTES
        move = None
        if start > 0:
            px0 = float(series.iloc[start - 1]["close"])
            px1 = float(series.iloc[min(i, n - 1)]["close"])
            move = ((px1 / px0) - 1.0) * 100.0
        if prev in ("BULLISH", "BEARISH") and nxt == prev:
            kind = "same_direction_resume"
        elif prev in ("BULLISH", "BEARISH") and nxt in ("BULLISH", "BEARISH") and nxt != prev:
            kind = "opposite_after_unclear"
        elif prev in ("BULLISH", "BEARISH") and nxt == "NONE":
            kind = "unclear_to_data_end"
        else:
            kind = "other"
        rows.append({
            "unclear_start_i": start,
            "unclear_end_i_excl": i,
            "unclear_start_utc": _iso_z(series.iloc[start]["close_ts"]),
            "prev_direction": prev,
            "next_direction": nxt,
            "classification": kind,
            "duration_minutes": dur,
            "price_move_pct": move,
            "le_5m": dur <= 5,
            "le_10m": dur <= 10,
            "le_15m": dur <= 15,
            "le_30m": dur <= 30,
            "le_60m": dur <= 60,
        })
    return pd.DataFrame(rows)


def signal_definitions(series: pd.DataFrame) -> dict[str, pd.DataFrame]:
    all_sig = extract_direction_signals(series)
    dirs = series["direction"].tolist()
    major_rows = []
    for _, s in all_sig.iterrows():
        prev = s["prev_direction"]
        d = s["signal_direction"]
        if prev in ("BULLISH", "BEARISH") and prev != d:
            major_rows.append(s.to_dict())
            continue
        if prev == "UNCLEAR":
            i = int(s["signal_index"]) - 1
            while i >= 0 and dirs[i] == "UNCLEAR":
                i -= 1
            last = dirs[i] if i >= 0 else "NONE"
            if last in ("BULLISH", "BEARISH") and last != d:
                major_rows.append(s.to_dict())
            elif last == "NONE":
                major_rows.append(s.to_dict())
    major_df = pd.DataFrame(major_rows) if major_rows else all_sig.iloc[0:0].copy()

    cluster_starts = []
    covered_until = -1
    for _, s in all_sig.iterrows():
        si = int(s["signal_index"])
        if si <= covered_until:
            continue
        d = s["signal_direction"]
        end = cluster_end_bridge_unclear(dirs, si, d, max_unclear_minutes=15)
        cluster_starts.append(s.to_dict())
        covered_until = end - 1
    cluster_df = pd.DataFrame(cluster_starts) if cluster_starts else all_sig.iloc[0:0].copy()
    return {
        "ALL_TRANSITIONS_TO_DIRECTION": all_sig,
        "MAJOR_FLIPS_ONLY": major_df,
        "CLUSTER_START_ONLY": cluster_df,
    }


def evaluate_signal_under_definition(series: pd.DataFrame, signal: dict[str, Any], *, end_mode: str) -> dict[str, Any]:
    dirs = series["direction"].tolist()
    i = int(signal["signal_index"])
    d = signal["signal_direction"]
    n = len(series)
    if end_mode == "RAW_EPISODE":
        end = raw_episode_end(dirs, i, d)
    elif end_mode == "SAME_DIRECTION_CLUSTER_15M":
        end = cluster_end_bridge_unclear(dirs, i, d, max_unclear_minutes=15)
    elif end_mode == "SAME_DIRECTION_CLUSTER_30M":
        end = cluster_end_bridge_unclear(dirs, i, d, max_unclear_minutes=30)
    elif end_mode == "UNTIL_OPPOSITE_CONFIRMED":
        end = until_opposite_end(dirs, i, d)
    else:
        raise ValueError(end_mode)

    entry, highs, lows, bars = forward_window(series, i, end)
    out = {
        **signal,
        "end_mode": end_mode,
        "end_index_excl": end,
        "episode_bars": bars,
        "episode_duration_minutes": bars * BAR_MINUTES if bars else 0,
        "entry_next_open": entry,
        "entry_next_open_utc": _iso_z(series.iloc[i + 1]["open_ts"]) if i + 1 < n and entry is not None else None,
        "evaluable": entry is not None,
    }
    meas = measure_window(d, entry, highs, lows) if entry is not None else {"high": None, "low": None, "mfe_pct": None, "mae_pct": None}
    out.update({"episode_high": meas["high"], "episode_low": meas["low"], "episode_mfe_pct": meas["mfe_pct"], "episode_mae_pct": meas["mae_pct"]})

    if entry is not None and i + 1 < n:
        max_h = max(FIXED_HORIZONS) // BAR_MINUTES
        fwd = series.iloc[i + 1 : min(n, i + 1 + max_h)]
        fh = fwd["high"].to_numpy(dtype=float)
        fl = fwd["low"].to_numpy(dtype=float)
        for h in FIXED_HORIZONS:
            b = h // BAR_MINUTES
            mfe, mae = mfe_mae_pct(direction=d, entry=entry, highs=fh, lows=fl, bars=b)
            out[f"mfe_{h}m_pct"] = mfe
            out[f"mae_{h}m_pct"] = mae
            for thr in THRESHOLDS:
                t = first_touch(direction=d, entry=entry, threshold=thr, highs=fh, lows=fl, max_bars=b)
                label = f"{thr*100:.2f}".replace(".", "p")
                out[f"fav_hit_{label}pct_within_{h}m"] = bool(t["favorable_hit"])
        for thr in THRESHOLDS:
            t = first_touch(direction=d, entry=entry, threshold=thr, highs=highs, lows=lows, max_bars=None)
            label = f"{thr*100:.2f}".replace(".", "p")
            out[f"episode_first_hit_{label}pct"] = t["first_hit"]
            out[f"episode_minutes_to_fav_{label}pct"] = t["minutes_to_favorable"]
            if t["favorable_hit_bar"] is not None:
                fb = int(t["favorable_hit_bar"]) + 1
                _, mae_before = mfe_mae_pct(direction=d, entry=entry, highs=highs, lows=lows, bars=fb)
                out[f"mae_before_fav_{label}pct"] = mae_before
            else:
                out[f"mae_before_fav_{label}pct"] = None
    return out


def manual_recalculate_case(series: pd.DataFrame, decision_time: str, expect_dir: str) -> dict[str, Any]:
    match = series[series["close_ts"].map(_iso_z) == decision_time]
    if match.empty:
        return {"decision_time": decision_time, "error": "signal bar not found"}
    row = match.iloc[0]
    i = int(row["i"])
    d = row["direction"]
    dirs = series["direction"].tolist()
    n = len(series)
    entry = float(series.iloc[i + 1]["open"]) if i + 1 < n else None
    entry_ts = series.iloc[i + 1]["open_ts"] if i + 1 < n else None
    raw_end = raw_episode_end(dirs, i, d)
    _, rh, rl, _ = forward_window(series, i, raw_end)
    raw = measure_window(d, entry, rh, rl) if entry else {}

    def fixed(minutes: int):
        if entry is None or i + 1 >= n:
            return {}
        b = minutes // BAR_MINUTES
        fwd = series.iloc[i + 1 : min(n, i + 1 + b)]
        if not len(fwd):
            return {}
        hi, lo = float(fwd["high"].max()), float(fwd["low"].min())
        mfe, mae = mfe_mae_from_hl(d, entry, hi, lo)
        return {"high": hi, "low": lo, "mfe_pct": mfe, "mae_pct": mae}

    f60, f240 = fixed(60), fixed(240)
    touches = {}
    if entry is not None and i + 1 < n:
        fwd = series.iloc[i + 1 : min(n, i + 1 + (480 // BAR_MINUTES))]
        fh, fl = fwd["high"].to_numpy(dtype=float), fwd["low"].to_numpy(dtype=float)
        for thr in (0.0025, 0.005, 0.01):
            t = first_touch(direction=d, entry=entry, threshold=thr, highs=fh, lows=fl, max_bars=None)
            tag = f"{thr*100:.2f}"
            touches[f"favorable_{tag}pct"] = {"hit": t["favorable_hit"], "minutes": t["minutes_to_favorable"], "target": t["favorable_target"], "first_hit": t["first_hit"]}
            touches[f"adverse_{tag}pct"] = {"hit": t["adverse_hit"], "minutes": t["minutes_to_adverse"], "target": t["adverse_target"]}

    formula = None
    if entry is not None and raw.get("low") is not None:
        if d == "BEARISH":
            formula = {
                "entry": entry,
                "future_low": raw["low"],
                "future_high": raw["high"],
                "favorable_move_decimal": (entry - raw["low"]) / entry,
                "favorable_move_pct": ((entry - raw["low"]) / entry) * 100.0,
                "adverse_move_decimal": (raw["high"] - entry) / entry,
                "adverse_move_pct": ((raw["high"] - entry) / entry) * 100.0,
                "note": "bearish favorable = (entry-low)/entry",
            }
        else:
            formula = {
                "entry": entry,
                "future_high": raw["high"],
                "future_low": raw["low"],
                "favorable_move_decimal": (raw["high"] - entry) / entry,
                "favorable_move_pct": ((raw["high"] - entry) / entry) * 100.0,
                "adverse_move_decimal": (entry - raw["low"]) / entry,
                "adverse_move_pct": ((entry - raw["low"]) / entry) * 100.0,
                "note": "bullish favorable = (high-entry)/entry",
            }

    return {
        "decision_time": decision_time,
        "expected_direction": expect_dir,
        "observed_direction": d,
        "direction_match": d == expect_dir,
        "signal_candle_close": _iso_z(row["close_ts"]),
        "signal_candle_close_price": float(row["close"]),
        "entry_next_open_time": _iso_z(entry_ts) if entry_ts is not None else None,
        "entry_next_open_price": entry,
        "raw_episode_end_utc": _iso_z(series.iloc[min(raw_end, n - 1)]["close_ts"]),
        "raw_episode_end_index_excl": raw_end,
        "raw_episode_low": raw.get("low"),
        "raw_episode_high": raw.get("high"),
        "raw_episode_mfe_pct": raw.get("mfe_pct"),
        "raw_episode_mae_pct": raw.get("mae_pct"),
        "60m_low": f60.get("low"),
        "60m_high": f60.get("high"),
        "mfe_60m_pct": f60.get("mfe_pct"),
        "mae_60m_pct": f60.get("mae_pct"),
        "240m_low": f240.get("low"),
        "240m_high": f240.get("high"),
        "mfe_240m_pct": f240.get("mfe_pct"),
        "mae_240m_pct": f240.get("mae_pct"),
        "touches_json": json.dumps(touches),
        "formula_json": json.dumps(formula),
        **{f"touch_{k}": json.dumps(v) for k, v in touches.items()},
    }


def check_invariants(rows: list[dict[str, Any]]) -> pd.DataFrame:
    viol = []
    for r in rows:
        sid = f"{r.get('symbol')}:{r.get('decision_time_utc')}:{r.get('end_mode')}"
        for key, inv in (("episode_mfe_pct", "mfe>=0"), ("episode_mae_pct", "mae>=0")):
            v = r.get(key)
            if v is not None and v < -1e-9:
                viol.append({"id": sid, "invariant": inv, "value": v})
        prev_mfe = prev_mae = None
        for h in FIXED_HORIZONS:
            mf, ma = r.get(f"mfe_{h}m_pct"), r.get(f"mae_{h}m_pct")
            if mf is not None and prev_mfe is not None and mf + 1e-9 < prev_mfe:
                viol.append({"id": sid, "invariant": f"mfe_{h}m_monotonic", "value": mf, "prev": prev_mfe})
            if ma is not None and prev_mae is not None and ma + 1e-9 < prev_mae:
                viol.append({"id": sid, "invariant": f"mae_{h}m_monotonic", "value": ma, "prev": prev_mae})
            if mf is not None:
                prev_mfe = mf
            if ma is not None:
                prev_mae = ma
        if r.get("entry_next_open_utc") and r.get("decision_time_utc"):
            if pd.Timestamp(r["entry_next_open_utc"]) < pd.Timestamp(r["decision_time_utc"]):
                viol.append({"id": sid, "invariant": "entry_after_signal", "value": r["entry_next_open_utc"]})
    return pd.DataFrame(viol)


def summarize_block(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"n": 0}
    ev = df[df["evaluable"] == True]  # noqa: E712
    def med(col):
        if col not in ev or ev.empty:
            return None
        s = pd.to_numeric(ev[col], errors="coerce").dropna()
        return float(s.median()) if len(s) else None
    def q(col, p):
        if col not in ev or ev.empty:
            return None
        s = pd.to_numeric(ev[col], errors="coerce").dropna()
        return float(s.quantile(p)) if len(s) else None
    def rate(col, val):
        if col not in ev or ev.empty:
            return None
        return float((ev[col] == val).mean())
    out = {
        "n": int(len(df)),
        "evaluable": int(len(ev)),
        "median_duration_minutes": med("episode_duration_minutes"),
        "median_mfe": med("episode_mfe_pct"),
        "median_mae": med("episode_mae_pct"),
        "p75_mfe": q("episode_mfe_pct", 0.75),
        "p90_mfe": q("episode_mfe_pct", 0.90),
    }
    for h in FIXED_HORIZONS:
        out[f"median_mfe_{h}m"] = med(f"mfe_{h}m_pct")
        out[f"p75_mfe_{h}m"] = q(f"mfe_{h}m_pct", 0.75)
        out[f"p90_mfe_{h}m"] = q(f"mfe_{h}m_pct", 0.90)
        out[f"median_mae_{h}m"] = med(f"mae_{h}m_pct")
        out[f"p75_mae_{h}m"] = q(f"mae_{h}m_pct", 0.75)
        out[f"p90_mae_{h}m"] = q(f"mae_{h}m_pct", 0.90)
    for thr in THRESHOLDS:
        label = f"{thr*100:.2f}".replace(".", "p")
        col = f"episode_first_hit_{label}pct"
        out[f"target_first_{label}pct"] = rate(col, "FAVORABLE")
        out[f"stop_first_{label}pct"] = rate(col, "ADVERSE")
        out[f"ambiguous_{label}pct"] = rate(col, "SAME_CANDLE_AMBIGUOUS")
        out[f"neither_{label}pct"] = rate(col, "NONE")
        for h in FIXED_HORIZONS:
            hc = f"fav_hit_{label}pct_within_{h}m"
            if hc in ev:
                out[f"fav_hit_{label}pct_within_{h}m"] = float(ev[hc].fillna(False).astype(bool).mean())
    return out


def build_apt_day_reconstruction(series: pd.DataFrame) -> pd.DataFrame:
    dirs = series["direction"].tolist()
    sigs = extract_direction_signals(series)
    if sigs.empty:
        return pd.DataFrame()
    ts = pd.to_datetime(sigs["decision_time_utc"], utc=True)
    sigs = sigs[(ts >= "2026-04-11") & (ts < "2026-04-12")]
    rows = []
    cluster_id = 0
    covered = -1
    marked = {c[0] for c in MANUAL_APT_CASES}
    for raw_id, (_, s) in enumerate(sigs.iterrows()):
        si = int(s["signal_index"])
        d = s["signal_direction"]
        is_start = si > covered
        if is_start:
            cluster_id += 1
            covered = cluster_end_bridge_unclear(dirs, si, d, max_unclear_minutes=15) - 1
        entry, _, _, _ = forward_window(series, si, len(series))
        modes = {
            "raw": raw_episode_end(dirs, si, d),
            "cluster_15m": cluster_end_bridge_unclear(dirs, si, d, max_unclear_minutes=15),
            "cluster_30m": cluster_end_bridge_unclear(dirs, si, d, max_unclear_minutes=30),
            "until_opposite": until_opposite_end(dirs, si, d),
        }
        rec = {
            "cluster_id": cluster_id if is_start else None,
            "raw_episode_id": raw_id,
            "is_cluster_15m_start": is_start,
            "direction": d,
            "start_time": s["decision_time_utc"],
            "start_price": s["signal_price_close"],
            "entry_next_open": entry,
            "marked_case": s["decision_time_utc"] in marked,
        }
        for name, end in modes.items():
            e, hi, lo, bars = forward_window(series, si, end)
            m = measure_window(d, e, hi, lo) if e is not None else {}
            rec[f"{name}_end_time"] = _iso_z(series.iloc[min(end, len(series) - 1)]["close_ts"])
            rec[f"{name}_low"] = m.get("low")
            rec[f"{name}_high"] = m.get("high")
            rec[f"{name}_mfe_pct"] = m.get("mfe_pct")
            rec[f"{name}_duration_m"] = bars * BAR_MINUTES
        if entry is not None and si + 1 < len(series):
            for h in (60, 240):
                b = h // BAR_MINUTES
                fwd = series.iloc[si + 1 : min(len(series), si + 1 + b)]
                if len(fwd):
                    mfe, _ = mfe_mae_from_hl(d, entry, float(fwd["high"].max()), float(fwd["low"].min()))
                    rec[f"mfe_{h}m_pct"] = mfe
                    rec[f"lowest_low_{h}m"] = float(fwd["low"].min())
                    rec[f"highest_high_{h}m"] = float(fwd["high"].max())
        rows.append(rec)
    return pd.DataFrame(rows)


def run_symbol_measurement_audit(*, symbol: str, candles=None, exchange: str = "bybit", env_file=None) -> dict[str, Any]:
    from research.regime_scanner.candle_sources import MySQLCandleSource, load_regime_db_env_file

    sym = normalize_symbol(symbol)
    if candles is None:
        if env_file:
            load_regime_db_env_file(Path(env_file))
        else:
            load_regime_db_env_file()
        src = MySQLCandleSource(exchange_default=exchange)
        try:
            candles = src.load_candles(exchange=exchange, symbol=sym, timeframe="5m", closed_only=True)
        finally:
            src.close()

    series = build_direction_series(run_c34b_on_ohlcv(_candles_to_ohlcv(candles)))
    unclear = classify_unclear_transitions(series)
    defs = signal_definitions(series)
    end_modes = ["RAW_EPISODE", "SAME_DIRECTION_CLUSTER_15M", "SAME_DIRECTION_CLUSTER_30M", "UNTIL_OPPOSITE_CONFIRMED"]

    evaluated = {m: [] for m in end_modes}
    for _, s in defs["ALL_TRANSITIONS_TO_DIRECTION"].iterrows():
        sig = s.to_dict(); sig["symbol"] = sym
        for mode in end_modes:
            evaluated[mode].append(evaluate_signal_under_definition(series, sig, end_mode=mode))

    cross = []
    for def_name, sdf in defs.items():
        for mode in ("RAW_EPISODE", "SAME_DIRECTION_CLUSTER_15M", "UNTIL_OPPOSITE_CONFIRMED"):
            for _, s in sdf.iterrows():
                sig = s.to_dict(); sig["symbol"] = sym
                r = evaluate_signal_under_definition(series, sig, end_mode=mode)
                r["signal_definition"] = def_name
                cross.append(r)

    paired_viol = []
    for a, b in zip(evaluated["RAW_EPISODE"], evaluated["SAME_DIRECTION_CLUSTER_15M"]):
        rm, cm = a.get("episode_mfe_pct"), b.get("episode_mfe_pct")
        if rm is not None and cm is not None and cm + 1e-9 < rm:
            paired_viol.append({"id": a.get("decision_time_utc"), "invariant": "cluster15_mfe>=raw_mfe", "raw": rm, "cluster15": cm, "symbol": sym})
        if b["end_index_excl"] < a["end_index_excl"]:
            paired_viol.append({"id": a.get("decision_time_utc"), "invariant": "cluster_end>=raw_end", "symbol": sym})
    for a, b in zip(evaluated["RAW_EPISODE"], evaluated["UNTIL_OPPOSITE_CONFIRMED"]):
        if b["end_index_excl"] < a["end_index_excl"]:
            paired_viol.append({"id": a.get("decision_time_utc"), "invariant": "until_opp_end>=raw_end", "symbol": sym})

    inv = check_invariants(sum(evaluated.values(), []))
    if paired_viol:
        inv = pd.concat([inv, pd.DataFrame(paired_viol)], ignore_index=True)

    summaries = {mode: summarize_block(pd.DataFrame(rows)) for mode, rows in evaluated.items()}
    for def_name in defs:
        for mode in ("RAW_EPISODE", "SAME_DIRECTION_CLUSTER_15M", "UNTIL_OPPOSITE_CONFIRMED"):
            sub = [r for r in cross if r.get("signal_definition") == def_name and r.get("end_mode") == mode]
            summaries[f"{def_name}__{mode}"] = summarize_block(pd.DataFrame(sub))

    n_unc = len(unclear)
    resume = unclear[unclear["classification"] == "same_direction_resume"] if n_unc else unclear
    opposite = unclear[unclear["classification"] == "opposite_after_unclear"] if n_unc else unclear

    return {
        "symbol": sym,
        "series": series,
        "unclear": unclear,
        "defs": defs,
        "evaluated": {k: pd.DataFrame(v) for k, v in evaluated.items()},
        "cross": pd.DataFrame(cross),
        "summaries": summaries,
        "invariants": inv,
        "unclear_stats": {
            "unclear_streaks": n_unc,
            "same_direction_resume": int(len(resume)),
            "opposite_after_unclear": int(len(opposite)),
            "same_direction_resume_rate": float(len(resume) / n_unc) if n_unc else None,
            "opposite_after_unclear_rate": float(len(opposite) / n_unc) if n_unc else None,
            "share_le_5m": float(unclear["le_5m"].mean()) if n_unc else None,
            "share_le_15m": float(unclear["le_15m"].mean()) if n_unc else None,
            "share_le_30m": float(unclear["le_30m"].mean()) if n_unc else None,
            "share_le_60m": float(unclear["le_60m"].mean()) if n_unc else None,
            "median_unclear_duration_m": float(unclear["duration_minutes"].median()) if n_unc else None,
        },
        "signal_counts": {k: int(len(v)) for k, v in defs.items()},
    }
