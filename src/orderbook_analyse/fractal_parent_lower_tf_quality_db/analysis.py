"""Run DB-only lower-TF quality-rank research."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import (
    coverage_audit,
    load_mysql_ohlcv_tf,
)
from orderbook_analyse.fractal_parent_lower_tf_quality_db import (
    APT_IS_END,
    AUDIT_VERSION,
    ENV_FILE,
    FEE_PCT,
    FIXED_TPSL,
    HORIZONS,
    LOWER_TFS,
    MAX_HOLD_MIN,
    MIN_SAMPLE,
    PARENT_TFS,
    QUALITY_CLASSES,
    QUALITY_RULE_DOC,
    SIZE_WEIGHTS,
    SYMBOLS,
    VERY_SMALL,
)
from orderbook_analyse.fractal_parent_lower_tf_quality_db.db_build import (
    attach_lower_tf_quality,
    build_tier_a_parents,
    build_waves_from_db,
    frozen_eff_edges_from_apt_db,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def sample_flag(n: int) -> str:
    if n < VERY_SMALL:
        return "VERY_SMALL_SAMPLE"
    if n < MIN_SAMPLE:
        return "SMALL_SAMPLE"
    return "OK"


def resolve_entries(events: pd.DataFrame, open_times: np.ndarray, opens: np.ndarray) -> pd.DataFrame:
    out = events.copy()
    conf = pd.to_datetime(out["confirmation_available_at"], utc=True).to_numpy(dtype="datetime64[ns]")
    idx = np.searchsorted(open_times, conf, side="right").astype(np.int64)
    n = len(open_times)
    valid = (idx >= 0) & (idx < n)
    # also require 1m bar exists for horizon
    px = np.full(len(out), np.nan)
    et = np.full(len(out), np.datetime64("NaT"), dtype="datetime64[ns]")
    px[valid] = opens[idx[valid]]
    et[valid] = open_times[idx[valid]]
    out["entry_i"] = np.where(valid, idx, -1)
    out["entry_price"] = px
    out["entry_time"] = pd.to_datetime(et, utc=True)
    out["entry_valid"] = valid & np.isfinite(px) & (px > 0)
    return out


def path_metrics(ei, epx, side, high, low, close, open_times, horizons) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if ei < 0 or epx <= 0:
        return out
    n = len(close)
    sign = -1.0 if side == "SHORT" else 1.0
    for h in horizons:
        t_h = open_times[ei] + np.timedelta64(int(h), "m")
        i_h = int(np.searchsorted(open_times, t_h, side="right") - 1)
        if i_h <= ei or i_h >= n:
            out[f"dir_ret_{h}m"] = np.nan
            out[f"mfe_{h}m"] = np.nan
            out[f"mae_{h}m"] = np.nan
            continue
        raw = (float(close[i_h]) / epx - 1.0) * 100.0
        hh = high[ei + 1 : i_h + 1]
        ll = low[ei + 1 : i_h + 1]
        if hh.size == 0:
            fav = adv = np.nan
        else:
            up = (float(np.max(hh)) / epx - 1.0) * 100.0
            dn = (float(np.min(ll)) / epx - 1.0) * 100.0
            fav, adv = (up, dn) if side == "LONG" else (-dn, -up)
        out[f"dir_ret_{h}m"] = raw * sign
        out[f"mfe_{h}m"] = fav
        out[f"mae_{h}m"] = adv
    # reach on max horizon
    h_max = max(horizons)
    t_h = open_times[ei] + np.timedelta64(int(h_max), "m")
    i_h = min(n - 1, max(ei + 1, int(np.searchsorted(open_times, t_h, side="right") - 1)))
    hh = high[ei + 1 : i_h + 1]
    ll = low[ei + 1 : i_h + 1]
    if hh.size:
        if side == "LONG":
            fav = (hh / epx - 1.0) * 100.0
        else:
            fav = (epx - ll) / epx * 100.0
        for lvl in (2.0, 3.0, 4.0, 6.0):
            out[f"reach_{lvl:g}pct"] = bool(np.any(fav >= lvl))
    return out


def sim_tpsl(ei, epx, side, high, low, close, open_times, tp, sl, max_hold) -> dict[str, Any]:
    if ei < 0 or epx <= 0:
        return {"exit_type": "INVALID", "net": np.nan, "gross": np.nan, "hold_min": np.nan}
    n = len(close)
    t_end = open_times[ei] + np.timedelta64(int(max_hold), "m")
    end_i = min(n - 1, max(ei + 1, int(np.searchsorted(open_times, t_end, side="right") - 1)))
    hh = high[ei + 1 : end_i + 1]
    ll = low[ei + 1 : end_i + 1]
    cc = close[ei + 1 : end_i + 1]
    hold = ((open_times[ei + 1 : end_i + 1] - open_times[ei]) / np.timedelta64(1, "m")).astype(float)
    if hh.size == 0:
        return {"exit_type": "INVALID", "net": np.nan, "gross": np.nan, "hold_min": np.nan}
    if side == "LONG":
        fav = (hh / epx - 1.0) * 100.0
        adv = (ll / epx - 1.0) * 100.0
        raw = (cc / epx - 1.0) * 100.0
    else:
        fav = (epx - ll) / epx * 100.0
        adv = -((hh - epx) / epx * 100.0)
        raw = -((cc / epx - 1.0) * 100.0)
    i_tp = int(np.argmax(fav >= tp)) if np.any(fav >= tp) else -1
    if not np.any(fav >= tp):
        i_tp = -1
    i_sl = int(np.argmax(adv <= -sl)) if np.any(adv <= -sl) else -1
    if not np.any(adv <= -sl):
        i_sl = -1
    if i_tp < 0 and i_sl < 0:
        g = float(raw[-1])
        return {"exit_type": "TIMEOUT", "gross": g, "net": g - FEE_PCT, "hold_min": float(hold[-1])}
    if i_tp < 0 or (i_sl >= 0 and i_sl <= i_tp):  # SL_FIRST incl same bar
        return {
            "exit_type": "SL",
            "gross": float(-sl),
            "net": float(-(sl + FEE_PCT)),
            "hold_min": float(hold[i_sl]),
        }
    return {
        "exit_type": "TP",
        "gross": float(tp),
        "net": float(tp - FEE_PCT),
        "hold_min": float(hold[i_tp]),
    }


def summarize_horizon(sub: pd.DataFrame, h: int, **meta) -> dict[str, Any]:
    col = f"dir_ret_{h}m"
    n = len(sub)
    row = {**meta, "horizon_min": h, "n": n, "sample_flag": sample_flag(n)}
    if n == 0 or col not in sub.columns:
        return row
    x = sub[col].astype(float).to_numpy()
    x = x[np.isfinite(x)]
    if not len(x):
        return row
    row.update(
        {
            "hit_rate": float(np.mean(x > 0)),
            "mean_dir_ret": float(np.mean(x)),
            "median_dir_ret": float(np.median(x)),
        }
    )
    for name, c in (("mfe", f"mfe_{h}m"), ("mae", f"mae_{h}m")):
        if c in sub.columns:
            v = sub[c].astype(float).to_numpy()
            v = v[np.isfinite(v)]
            if len(v):
                row[f"median_{name}"] = float(np.median(v))
                row[f"mean_{name}"] = float(np.mean(v))
    return row


def summarize_tpsl(nets, exits, holds, **meta) -> dict[str, Any]:
    n = len(nets)
    row = {**meta, "n": n, "sample_flag": sample_flag(n)}
    if n == 0:
        return row
    nets = np.asarray(nets, float)
    exits = np.asarray(exits, object)
    holds = np.asarray(holds, float)
    wins = nets[nets > 0]
    losses = nets[nets < 0]
    eq = np.cumsum(nets)
    dd = eq - np.maximum.accumulate(eq)
    row.update(
        {
            "expectancy": float(np.mean(nets)),
            "median_net": float(np.median(nets)),
            "tp_rate": float(np.mean(exits == "TP")),
            "sl_rate": float(np.mean(exits == "SL")),
            "win_rate": float(np.mean(nets > 0)),
            "avg_winner": float(np.mean(wins)) if len(wins) else None,
            "avg_loser": float(np.mean(losses)) if len(losses) else None,
            "profit_factor": (
                float(np.sum(wins) / abs(np.sum(losses)))
                if len(wins) and len(losses) and np.sum(losses) != 0
                else None
            ),
            "max_drawdown": float(dd.min()) if len(dd) else None,
            "median_hold_min": float(np.median(holds)),
            "cumulative_net": float(np.sum(nets)),
        }
    )
    return row


def monotonicity(values_by_class: dict[str, float | None], *, higher_better: bool = True) -> str:
    order = ["A_PLUS_TIMING", "A_TIMING", "A_MINUS_TIMING"]
    vals = [values_by_class.get(c) for c in order]
    if any(v is None for v in vals):
        return "INSUFFICIENT"
    if higher_better:
        if vals[0] > vals[1] > vals[2]:
            return "MONOTONIC"
        if vals[0] >= vals[2] and (vals[0] >= vals[1] or vals[1] >= vals[2]):
            return "MOSTLY_MONOTONIC"
        return "NON_MONOTONIC"
    if vals[0] < vals[1] < vals[2]:
        return "MONOTONIC"
    if vals[0] <= vals[2] and (vals[0] <= vals[1] or vals[1] <= vals[2]):
        return "MOSTLY_MONOTONIC"
    return "NON_MONOTONIC"


def run_analysis() -> dict[str, Any]:
    load_env_file(ENV_FILE)
    print("[db] inventory …", flush=True)
    inventory = {}
    for sym in ("DOGEUSDT", "BTCUSDT", "APTUSDT"):
        inventory[sym] = coverage_audit(
            symbol=sym, timeframes=("1m", "5m", "15m", "30m", "1h", "4h"), env_file=ENV_FILE
        )
    print("[db] no cycle_waves table — waves computed in-memory from market_candles", flush=True)
    print("[edges] APT-IS efficiency quartiles from MySQL …", flush=True)
    edges = frozen_eff_edges_from_apt_db()

    quality_rows: list[dict] = []
    tpsl_rows: list[dict] = []
    cross_rows: list[dict] = []
    mono_rows: list[dict] = []
    sizing_rows: list[dict] = []
    reach_rows: list[dict] = []
    freq_rows: list[dict] = []

    # collect per-key summaries for cross/decisions
    horizon_index: dict[tuple, dict] = {}

    for sym in SYMBOLS:
        print(f"\n===== {sym} =====", flush=True)
        needed = set(PARENT_TFS)
        for ptf in PARENT_TFS:
            needed.update(LOWER_TFS[ptf])
        waves_by_tf = {tf: build_waves_from_db(sym, tf) for tf in sorted(needed)}
        c1 = load_mysql_ohlcv_tf(symbol=sym, timeframe="1m", env_file=ENV_FILE)
        high = c1["high"].astype(float).to_numpy()
        low = c1["low"].astype(float).to_numpy()
        close = c1["close"].astype(float).to_numpy()
        opens = c1["open"].astype(float).to_numpy()
        open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")
        t_min = open_times[0] if len(open_times) else None
        t_max = open_times[-1] if len(open_times) else None

        for ptf in PARENT_TFS:
            print(f"[parent] {sym} {ptf}", flush=True)
            parents = build_tier_a_parents(sym, ptf, edges, waves=waves_by_tf[ptf])
            if parents.empty:
                continue
            parents = attach_lower_tf_quality(parents, waves_by_tf, ptf)
            parents = resolve_entries(parents, open_times, opens)
            # drop entries outside 1m coverage
            if t_min is not None:
                et = parents["entry_time"].to_numpy(dtype="datetime64[ns]")
                parents.loc[parents["entry_valid"] & ((et < t_min) | (et > t_max)), "entry_valid"] = False

            horizons = HORIZONS[ptf]
            records = []
            for ev in parents.itertuples(index=False):
                if not bool(getattr(ev, "entry_valid", False)):
                    continue
                po = path_metrics(
                    int(ev.entry_i),
                    float(ev.entry_price),
                    str(ev.side),
                    high,
                    low,
                    close,
                    open_times,
                    horizons,
                )
                rec = {
                    "symbol": sym,
                    "timeframe": ptf,
                    "side": str(ev.side),
                    "quality_class": str(ev.quality_class),
                    "exhausted_count": int(ev.exhausted_count),
                    "favorable_count": int(ev.favorable_count),
                    "entry_time": pd.Timestamp(ev.entry_time),
                    **po,
                }
                for tp, sl in FIXED_TPSL[ptf]:
                    sim = sim_tpsl(
                        int(ev.entry_i),
                        float(ev.entry_price),
                        str(ev.side),
                        high,
                        low,
                        close,
                        open_times,
                        tp,
                        sl,
                        MAX_HOLD_MIN[ptf],
                    )
                    rec[f"net_tp{tp:g}_sl{sl:g}"] = sim["net"]
                    rec[f"exit_tp{tp:g}_sl{sl:g}"] = sim["exit_type"]
                    rec[f"hold_tp{tp:g}_sl{sl:g}"] = sim["hold_min"]
                records.append(rec)

            df = pd.DataFrame(records)
            if df.empty:
                continue
            print(f"[valid entries] {sym} {ptf}: n={len(df)}", flush=True)

            # frequency
            et = pd.to_datetime(df["entry_time"], utc=True)
            months = max(1.0, (et.max() - et.min()).days / 30.44)
            n_all = len(df)
            for q in QUALITY_CLASSES:
                nq = int((df.quality_class == q).sum())
                freq_rows.append(
                    {
                        "symbol": sym,
                        "timeframe": ptf,
                        "quality_class": q,
                        "n": nq,
                        "retained_share": nq / n_all if n_all else None,
                        "signals_per_month": nq / months,
                        "signals_per_year": nq / months * 12,
                    }
                )

            for side in ("LONG", "SHORT", "COMBINED"):
                sdf = df if side == "COMBINED" else df[df.side == side]
                # ALL baseline
                for h in horizons:
                    row = summarize_horizon(
                        sdf, h, symbol=sym, timeframe=ptf, side=side, quality_class="ALL_TIER_A"
                    )
                    quality_rows.append(row)
                    horizon_index[(sym, ptf, side, "ALL_TIER_A", h)] = row
                for q in QUALITY_CLASSES:
                    g = sdf[sdf.quality_class == q]
                    for h in horizons:
                        row = summarize_horizon(
                            g, h, symbol=sym, timeframe=ptf, side=side, quality_class=q
                        )
                        quality_rows.append(row)
                        horizon_index[(sym, ptf, side, q, h)] = row

                    # reach rates
                    for lvl in ((2.0, 3.0) if ptf == "1h" else (4.0, 6.0)):
                        rc = f"reach_{lvl:g}pct"
                        if rc in g.columns and len(g):
                            reach_rows.append(
                                {
                                    "symbol": sym,
                                    "timeframe": ptf,
                                    "side": side,
                                    "quality_class": q,
                                    "tp_level": lvl,
                                    "n": len(g),
                                    "reach_rate": float(g[rc].astype(bool).mean()),
                                    "sample_flag": sample_flag(len(g)),
                                }
                            )

                # TPSL by quality
                for tp, sl in FIXED_TPSL[ptf]:
                    nk = f"net_tp{tp:g}_sl{sl:g}"
                    ek = f"exit_tp{tp:g}_sl{sl:g}"
                    hk = f"hold_tp{tp:g}_sl{sl:g}"
                    for q in ("ALL_TIER_A", *QUALITY_CLASSES):
                        g = sdf if q == "ALL_TIER_A" else sdf[sdf.quality_class == q]
                        if g.empty:
                            continue
                        tpsl_rows.append(
                            summarize_tpsl(
                                g[nk].astype(float).to_numpy(),
                                g[ek].astype(str).to_numpy(),
                                g[hk].astype(float).to_numpy(),
                                symbol=sym,
                                timeframe=ptf,
                                side=side,
                                quality_class=q,
                                tp_pct=tp,
                                sl_pct=sl,
                            )
                        )

                # monotonicity on primary horizon
                h0 = horizons[0]
                for metric, higher in (
                    ("mean_dir_ret", True),
                    ("hit_rate", True),
                    ("median_mfe", True),
                    ("median_mae", False),  # less negative / higher better? mae is negative; higher (closer to 0) better
                ):
                    by_c = {
                        q: (horizon_index.get((sym, ptf, side, q, h0)) or {}).get(metric)
                        for q in QUALITY_CLASSES
                    }
                    # for MAE: higher (less adverse) is better if mae negative
                    mono_rows.append(
                        {
                            "symbol": sym,
                            "timeframe": ptf,
                            "side": side,
                            "metric": metric,
                            "horizon_min": h0,
                            **{f"v_{q}": by_c[q] for q in QUALITY_CLASSES},
                            "monotonicity": monotonicity(by_c, higher_better=higher),
                        }
                    )

            # sizing research COMBINED primary TPSL
            tp0, sl0 = FIXED_TPSL[ptf][0]
            nk = f"net_tp{tp0:g}_sl{sl0:g}"
            flat = df[nk].astype(float).to_numpy()
            wts = df["quality_class"].map(SIZE_WEIGHTS).astype(float).to_numpy()
            # chronological
            order = np.argsort(pd.to_datetime(df["entry_time"], utc=True).to_numpy())
            flat_o, wts_o = flat[order], wts[order]
            w_ret = flat_o * wts_o
            eq_f = np.cumsum(flat_o)
            eq_w = np.cumsum(w_ret)
            dd_f = eq_f - np.maximum.accumulate(eq_f)
            dd_w = eq_w - np.maximum.accumulate(eq_w)
            sizing_rows.append(
                {
                    "symbol": sym,
                    "timeframe": ptf,
                    "tp_pct": tp0,
                    "sl_pct": sl0,
                    "n": len(df),
                    "flat_mean_net": float(np.mean(flat_o)),
                    "weighted_mean_net": float(np.mean(w_ret)),
                    "flat_cum": float(np.sum(flat_o)),
                    "weighted_cum": float(np.sum(w_ret)),
                    "flat_maxDD": float(dd_f.min()) if len(dd_f) else None,
                    "weighted_maxDD": float(dd_w.min()) if len(dd_w) else None,
                }
            )

    # cross-symbol
    for ptf in PARENT_TFS:
        h0 = HORIZONS[ptf][0]
        for side in ("LONG", "SHORT"):
            for left, right, tag in (
                ("A_PLUS_TIMING", "A_MINUS_TIMING", "Aplus_vs_Aminus"),
                ("A_PLUS_TIMING", "ALL_TIER_A", "Aplus_vs_ALL"),
                ("A_MINUS_TIMING", "ALL_TIER_A", "Aminus_vs_ALL"),
            ):
                doge_l = horizon_index.get(("DOGEUSDT", ptf, side, left, h0))
                doge_r = horizon_index.get(("DOGEUSDT", ptf, side, right, h0))
                btc_l = horizon_index.get(("BTCUSDT", ptf, side, left, h0))
                btc_r = horizon_index.get(("BTCUSDT", ptf, side, right, h0))

                def lift(a, b):
                    if not a or not b:
                        return None
                    if a.get("mean_dir_ret") is None or b.get("mean_dir_ret") is None:
                        return None
                    return a["mean_dir_ret"] - b["mean_dir_ret"]

                dl, bl = lift(doge_l, doge_r), lift(btc_l, btc_r)
                if dl is None or bl is None:
                    cons = "INSUFFICIENT"
                elif dl > 0.05 and bl > 0.05:
                    cons = "REPLICATES"
                elif (dl > 0.05) != (bl > 0.05):
                    cons = "MIXED"
                elif dl < -0.05 and bl < -0.05:
                    cons = "CONTRADICTS"
                else:
                    cons = "MIXED"
                cross_rows.append(
                    {
                        "timeframe": ptf,
                        "side": side,
                        "comparison": tag,
                        "DOGE_lift_mean": dl,
                        "BTC_lift_mean": bl,
                        "DOGE_n_left": None if not doge_l else doge_l.get("n"),
                        "BTC_n_left": None if not btc_l else btc_l.get("n"),
                        "consistency": cons,
                    }
                )

    decisions, answers = _decide(
        quality_rows, tpsl_rows, mono_rows, sizing_rows, reach_rows, cross_rows, freq_rows
    )

    return {
        "audit_version": AUDIT_VERSION,
        "fee_pct": FEE_PCT,
        "apt_is_end": APT_IS_END,
        "db_inventory_note": (
            "MySQL tables relevant: market_candles only for waves/OHLCV. "
            "No cycle_waves / cycle_indicator_features tables. "
            "Waves/indicators computed in-memory via fractal_cycle_wave_analysis."
        ),
        "db_inventory": inventory,
        "frozen_eff_edges": {
            f"{tf}|{d}|{col}": v for (tf, d, col), v in edges.items()
        },
        "quality_rule": QUALITY_RULE_DOC.strip(),
        "quality_summary": quality_rows,
        "fixed_tpsl_by_quality": tpsl_rows,
        "cross_symbol_consistency": cross_rows,
        "monotonicity": mono_rows,
        "sizing_research": sizing_rows,
        "tp_reach_by_quality": reach_rows,
        "frequency": freq_rows,
        "decisions": decisions,
        "answers": answers,
    }


def _decide(quality_rows, tpsl_rows, mono_rows, sizing_rows, reach_rows, cross_rows, freq_rows):
    # Count REPLICATES A+ vs A-
    reps = [
        r
        for r in cross_rows
        if r.get("comparison") == "Aplus_vs_Aminus" and r.get("consistency") == "REPLICATES"
    ]
    mixed = [
        r
        for r in cross_rows
        if r.get("comparison") == "Aplus_vs_Aminus" and r.get("consistency") == "MIXED"
    ]
    mono_ok = [
        r
        for r in mono_rows
        if r.get("metric") == "mean_dir_ret"
        and r.get("side") in ("LONG", "SHORT")
        and r.get("monotonicity") in ("MONOTONIC", "MOSTLY_MONOTONIC")
    ]

    if len(reps) >= 2 and len(mono_ok) >= 2:
        primary = "LOWER_TF_QUALITY_RANK_HAS_VALUE"
    elif len(reps) >= 1 or len(mono_ok) >= 2 or len(mixed) >= 2:
        primary = "LOWER_TF_QUALITY_ONLY_WEAK_CONTEXT"
    else:
        primary = "LOWER_TF_QUALITY_NO_VALUE"

    # sizing: weighted improves mean or DD on both symbols for any TF
    size_help = 0
    for r in sizing_rows:
        if r.get("weighted_mean_net") is None or r.get("flat_mean_net") is None:
            continue
        # Require expectancy improvement; DD-only compression is not enough
        if r["weighted_mean_net"] > r["flat_mean_net"] + 0.02:
            size_help += 1
    size_dec = (
        "LOWER_TF_QUALITY_SUPPORTS_POSITION_SIZING"
        if size_help >= 2 and primary != "LOWER_TF_QUALITY_NO_VALUE"
        else "LOWER_TF_QUALITY_NOT_USEFUL_FOR_SIZING"
    )

    # TP selection: A+ reach higher than A- for large targets, replicated
    tp_help = 0
    for ptf, lvl in (("1h", 3.0), ("4h", 6.0)):
        for side in ("LONG", "SHORT"):
            doge_p = next(
                (
                    r
                    for r in reach_rows
                    if r["symbol"] == "DOGEUSDT"
                    and r["timeframe"] == ptf
                    and r["side"] == side
                    and r["quality_class"] == "A_PLUS_TIMING"
                    and r["tp_level"] == lvl
                    and r.get("sample_flag") == "OK"
                ),
                None,
            )
            doge_m = next(
                (
                    r
                    for r in reach_rows
                    if r["symbol"] == "DOGEUSDT"
                    and r["timeframe"] == ptf
                    and r["side"] == side
                    and r["quality_class"] == "A_MINUS_TIMING"
                    and r["tp_level"] == lvl
                ),
                None,
            )
            btc_p = next(
                (
                    r
                    for r in reach_rows
                    if r["symbol"] == "BTCUSDT"
                    and r["timeframe"] == ptf
                    and r["side"] == side
                    and r["quality_class"] == "A_PLUS_TIMING"
                    and r["tp_level"] == lvl
                ),
                None,
            )
            btc_m = next(
                (
                    r
                    for r in reach_rows
                    if r["symbol"] == "BTCUSDT"
                    and r["timeframe"] == ptf
                    and r["side"] == side
                    and r["quality_class"] == "A_MINUS_TIMING"
                    and r["tp_level"] == lvl
                ),
                None,
            )
            if doge_p and doge_m and (doge_p.get("reach_rate") or 0) > (doge_m.get("reach_rate") or 0) + 0.05:
                tp_help += 1
            if btc_p and btc_m and btc_p.get("sample_flag") == "OK" and (btc_p.get("reach_rate") or 0) > (
                btc_m.get("reach_rate") or 0
            ) + 0.05:
                tp_help += 1
    tp_dec = (
        "LOWER_TF_QUALITY_SUPPORTS_TP_SELECTION"
        if tp_help >= 2
        else "LOWER_TF_QUALITY_NOT_USEFUL_FOR_TP_SELECTION"
    )

    # A- still profitable?
    a_minus_pos = [
        r
        for r in tpsl_rows
        if r.get("quality_class") == "A_MINUS_TIMING"
        and r.get("side") == "COMBINED"
        and r.get("sample_flag") == "OK"
        and (r.get("expectancy") or 0) > 0
        and (r.get("profit_factor") or 0) > 1
    ]

    answers = {
        "A_Aplus_better_than_Aminus": {
            "replicates_count": len(reps),
            "mixed_count": len(mixed),
            "mono_mean_ret_ok": len(mono_ok),
        },
        "B_cross_symbol": cross_rows,
        "C_sizing": sizing_rows,
        "D_Aminus_still_ok_tpsl_positive": len(a_minus_pos),
        "E_later_use": (
            "POSITION_SIZE"
            if size_dec.startswith("LOWER_TF_QUALITY_SUPPORTS") and not tp_dec.startswith("LOWER_TF_QUALITY_SUPPORTS")
            else (
                "TP_TARGET"
                if tp_dec.startswith("LOWER_TF_QUALITY_SUPPORTS") and not size_dec.startswith("LOWER_TF_QUALITY_SUPPORTS")
                else (
                    "BOTH"
                    if size_dec.startswith("LOWER_TF_QUALITY_SUPPORTS")
                    and tp_dec.startswith("LOWER_TF_QUALITY_SUPPORTS")
                    else "NOTHING"
                )
            )
        ),
        "F_T0_for_all": "YES_KEEP_T0_ALL_CLASSES",
        "frequency_note": freq_rows[:20],
    }

    return (
        {"primary": primary, "sizing": size_dec, "tp_selection": tp_dec},
        answers,
    )
