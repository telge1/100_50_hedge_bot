"""TP/SL path simulation on 1m OHLC (path-precompute + grid)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_15m_failure_tpsl import (
    CONFIRMATION_EVENTS,
    ENTRY_DETAIL,
    FIRST_TOUCH_LEVELS,
    MAX_HOLD_MIN,
    MIN_SAMPLE,
    ROUNDTRIP_FEE_PCT,
    SL_GRID,
    TP_GRID,
)
from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf


def load_t0_entries() -> pd.DataFrame:
    d = pd.read_csv(
        ENTRY_DETAIL,
        usecols=[
            "wave_i",
            "failure_type",
            "side",
            "confirmation_available_at",
            "delay_min",
            "entry_time",
            "entry_price",
            "entry_i",
        ],
    )
    d = d[d["delay_min"] == 0].copy()
    d["entry_time"] = pd.to_datetime(d["entry_time"], utc=True)
    d["confirmation_available_at"] = pd.to_datetime(d["confirmation_available_at"], utc=True)
    feats = pd.read_csv(
        CONFIRMATION_EVENTS,
        usecols=[
            "wave_i",
            "M15_signed_price_move_pct",
            "M15_directional_efficiency",
            "wave_duration_min",
            "M15_rsi_end",
            "M15_stoch_k_start",
            "M15_stoch_k_end",
            "partial_fail_streak_1m",
        ],
    )
    return d.merge(feats, on="wave_i", how="left").reset_index(drop=True)


def load_1m() -> pd.DataFrame:
    raw = load_mysql_ohlcv_tf(symbol="APTUSDT", timeframe="1m")
    df = raw.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def build_trade_paths(
    entries: pd.DataFrame,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_times: np.ndarray,
) -> list[dict[str, Any]]:
    """Per entry: bar-level favorable/adverse % in trade direction."""
    paths: list[dict[str, Any]] = []
    n = len(close)
    for ev in entries.itertuples(index=False):
        ei = int(ev.entry_i)
        epx = float(ev.entry_price)
        side = str(ev.side)
        if ei < 0 or ei >= n - 1 or not np.isfinite(epx) or epx <= 0:
            paths.append({"valid": False, "ev": ev})
            continue
        end_i = min(n - 1, ei + MAX_HOLD_MIN)
        hh = high[ei + 1 : end_i + 1].astype(float)
        ll = low[ei + 1 : end_i + 1].astype(float)
        cc = close[ei + 1 : end_i + 1].astype(float)
        if side == "LONG":
            fav = (hh / epx - 1.0) * 100.0
            adv = (ll / epx - 1.0) * 100.0  # negative when adverse
        else:
            fav = (epx - ll) / epx * 100.0  # positive when price down
            adv = -((hh - epx) / epx * 100.0)  # negative when price up
        # raw directional close path for time exit
        if side == "LONG":
            raw_close = (cc / epx - 1.0) * 100.0
        else:
            raw_close = -((cc / epx - 1.0) * 100.0)
        hold_min = np.arange(1, len(fav) + 1, dtype=float)  # approx 1m per bar
        # more exact hold from timestamps
        hold_min = (
            (open_times[ei + 1 : end_i + 1] - open_times[ei]) / np.timedelta64(1, "m")
        ).astype(float)
        paths.append(
            {
                "valid": True,
                "ev": ev,
                "fav": fav,
                "adv": adv,
                "raw_close": raw_close,
                "hold_min": hold_min,
                "exit_indices": np.arange(ei + 1, end_i + 1),
            }
        )
    return paths


def resolve_on_path(
    path: dict[str, Any],
    *,
    tp_pct: float,
    sl_pct: float,
    policy: str,
) -> dict[str, Any]:
    if not path["valid"]:
        return {
            "exit_type": "INVALID",
            "gross_ret": np.nan,
            "net_ret": np.nan,
            "hold_min": np.nan,
            "ambiguous": False,
        }
    fav = path["fav"]
    adv = path["adv"]
    hit_tp = fav >= tp_pct
    hit_sl = adv <= -sl_pct
    both = hit_tp & hit_sl
    only_tp = hit_tp & ~hit_sl
    only_sl = hit_sl & ~hit_tp

    # first index of any hit
    any_hit = hit_tp | hit_sl
    if not np.any(any_hit):
        gross = float(path["raw_close"][-1])
        return {
            "exit_type": "TIME_EXIT",
            "gross_ret": gross,
            "net_ret": gross - ROUNDTRIP_FEE_PCT,
            "hold_min": float(path["hold_min"][-1]),
            "ambiguous": False,
        }

    i = int(np.argmax(any_hit))  # first True
    ambiguous = bool(both[i])
    if ambiguous:
        if policy == "TP_FIRST":
            return {
                "exit_type": "TP",
                "gross_ret": float(tp_pct),
                "net_ret": float(tp_pct - ROUNDTRIP_FEE_PCT),
                "hold_min": float(path["hold_min"][i]),
                "ambiguous": True,
            }
        return {
            "exit_type": "SL",
            "gross_ret": float(-sl_pct),
            "net_ret": float(-(sl_pct + ROUNDTRIP_FEE_PCT)),
            "hold_min": float(path["hold_min"][i]),
            "ambiguous": True,
        }
    if only_tp[i] or (hit_tp[i] and not hit_sl[i]):
        return {
            "exit_type": "TP",
            "gross_ret": float(tp_pct),
            "net_ret": float(tp_pct - ROUNDTRIP_FEE_PCT),
            "hold_min": float(path["hold_min"][i]),
            "ambiguous": False,
        }
    return {
        "exit_type": "SL",
        "gross_ret": float(-sl_pct),
        "net_ret": float(-(sl_pct + ROUNDTRIP_FEE_PCT)),
        "hold_min": float(path["hold_min"][i]),
        "ambiguous": False,
    }


def run_grid(paths: list[dict], *, policy: str) -> pd.DataFrame:
    rows = []
    combos = [(t, s) for t in TP_GRID for s in SL_GRID]
    for ci, (tp, sl) in enumerate(combos):
        if ci % 8 == 0:
            print(f"[grid] {policy} combo {ci+1}/{len(combos)} tp={tp} sl={sl}", flush=True)
        for path in paths:
            ev = path["ev"]
            sim = resolve_on_path(path, tp_pct=tp, sl_pct=sl, policy=policy)
            rows.append(
                {
                    "wave_i": int(ev.wave_i),
                    "side": ev.side,
                    "failure_type": ev.failure_type,
                    "entry_time": ev.entry_time,
                    "entry_price": float(ev.entry_price),
                    "tp_pct": float(tp),
                    "sl_pct": float(sl),
                    "policy": policy,
                    **sim,
                }
            )
    return pd.DataFrame(rows)


def summarize_combo(sub: pd.DataFrame, **meta) -> dict[str, Any]:
    n = int(len(sub))
    row: dict[str, Any] = {**meta, "n": n}
    if n == 0:
        return row
    et = sub["exit_type"].astype(str)
    net = sub["net_ret"].astype(float)
    gross = sub["gross_ret"].astype(float)
    hold = sub["hold_min"].astype(float)
    tp_n = int((et == "TP").sum())
    sl_n = int((et == "SL").sum())
    to_n = int((et == "TIME_EXIT").sum())
    amb_n = int(sub["ambiguous"].fillna(False).astype(bool).sum())
    wins = net[net > 0]
    losses = net[net < 0]
    row.update(
        {
            "tp_count": tp_n,
            "sl_count": sl_n,
            "time_exit_count": to_n,
            "ambiguous_count": amb_n,
            "tp_rate": tp_n / n,
            "sl_rate": sl_n / n,
            "timeout_rate": to_n / n,
            "ambiguous_rate": amb_n / n,
            "gross_total_return": float(gross.sum()),
            "net_total_return": float(net.sum()),
            "mean_net_return": float(net.mean()),
            "median_net_return": float(net.median()),
            "expectancy": float(net.mean()),
            "win_rate": float((net > 0).mean()),
            "loss_rate": float((net < 0).mean()),
            "avg_winner": float(wins.mean()) if len(wins) else None,
            "avg_loser": float(losses.mean()) if len(losses) else None,
            "payoff_ratio": (
                float(wins.mean() / abs(losses.mean()))
                if len(wins) and len(losses) and float(losses.mean()) != 0
                else None
            ),
            "profit_factor": (
                float(wins.sum() / abs(losses.sum()))
                if len(wins) and len(losses) and float(losses.sum()) != 0
                else None
            ),
            "cumulative_return": float(net.sum()),
            "worst_trade": float(net.min()),
            "best_trade": float(net.max()),
            "q05_net": float(net.quantile(0.05)),
            "q95_net": float(net.quantile(0.95)),
            "median_hold_min": float(hold.median()),
            "mean_hold_min": float(hold.mean()),
            "median_hold_tp": float(hold[et == "TP"].median()) if tp_n else None,
            "median_hold_sl": float(hold[et == "SL"].median()) if sl_n else None,
            "median_hold_time": float(hold[et == "TIME_EXIT"].median()) if to_n else None,
            "share_exit_le15": float((hold <= 15).mean()),
            "share_exit_le30": float((hold <= 30).mean()),
            "share_exit_le60": float((hold <= 60).mean()),
            "share_exit_le120": float((hold <= 120).mean()),
            "share_exit_le240": float((hold <= 240).mean()),
            "sample_flag": "OK" if n >= MIN_SAMPLE else "SMALL_SAMPLE",
        }
    )
    ordered = sub.sort_values("entry_time")
    eq = ordered["net_ret"].astype(float).cumsum()
    dd = eq - eq.cummax()
    row["max_drawdown"] = float(dd.min()) if len(dd) else None
    row["recovery_factor"] = (
        float(row["net_total_return"] / abs(row["max_drawdown"]))
        if row.get("max_drawdown") is not None and row["max_drawdown"] < 0
        else None
    )
    signs = np.sign(ordered["net_ret"].astype(float).to_numpy())
    max_w = max_l = cur_w = cur_l = 0
    for s in signs:
        if s > 0:
            cur_w += 1
            cur_l = 0
            max_w = max(max_w, cur_w)
        elif s < 0:
            cur_l += 1
            cur_w = 0
            max_l = max(max_l, cur_l)
        else:
            cur_w = cur_l = 0
    row["max_consecutive_wins"] = int(max_w)
    row["max_consecutive_losses"] = int(max_l)
    return row


def mae_mfe_summary(paths: list[dict]) -> list[dict]:
    records = []
    for path in paths:
        if not path["valid"]:
            continue
        side = str(path["ev"].side)
        fav = path["fav"]
        adv = path["adv"]
        if len(fav) == 0:
            continue
        mfe = float(np.max(fav))
        mae = float(np.min(adv))  # adverse, typically negative
        i_mfe = int(np.argmax(fav))
        i_mae = int(np.argmin(adv))
        if i_mfe < i_mae:
            order = "mfe_before_mae"
        elif i_mae < i_mfe:
            order = "mae_before_mfe"
        else:
            order = "same_bar"
        # first touch of +/-0.1% for structure
        first = "none"
        for f, a in zip(fav, adv):
            hf, ha = f >= 0.1, a <= -0.1
            if hf and ha:
                first = "both"
                break
            if hf:
                first = "mfe_first"
                break
            if ha:
                first = "mae_first"
                break
        records.append(
            {
                "side": side,
                "mfe": mfe,
                "mae": mae,
                "order_extreme": order,
                "first_01": first,
            }
        )
    rec = pd.DataFrame(records)
    rows = []
    for side in ("LONG", "SHORT", "COMBINED"):
        sub = rec if side == "COMBINED" else rec[rec["side"] == side]
        if sub.empty:
            continue
        for col in ("mfe", "mae"):
            s = sub[col].astype(float)
            rows.append(
                {
                    "side": side,
                    "metric": col,
                    "n": int(len(sub)),
                    "q10": float(s.quantile(0.10)),
                    "q25": float(s.quantile(0.25)),
                    "median": float(s.median()),
                    "q75": float(s.quantile(0.75)),
                    "q90": float(s.quantile(0.90)),
                    "share_mfe_before_mae": float((sub["order_extreme"] == "mfe_before_mae").mean()),
                    "share_mae_before_mfe": float((sub["order_extreme"] == "mae_before_mfe").mean()),
                    "share_mfe_before_mae_0_1": float((sub["first_01"] == "mfe_first").mean()),
                    "share_mae_before_mfe_0_1": float((sub["first_01"] == "mae_first").mean()),
                }
            )
    return rows


def first_touch_matrix(paths: list[dict]) -> list[dict]:
    counts = {
        side: {lvl: {"fav": 0, "adv": 0, "both": 0, "none": 0, "n": 0} for lvl in FIRST_TOUCH_LEVELS}
        for side in ("LONG", "SHORT")
    }
    for path in paths:
        if not path["valid"]:
            continue
        side = str(path["ev"].side)
        for lvl in FIRST_TOUCH_LEVELS:
            counts[side][lvl]["n"] += 1
            hit = "none"
            for f, a in zip(path["fav"], path["adv"]):
                hf, ha = f >= lvl, a <= -lvl
                if hf and ha:
                    hit = "both"
                    break
                if hf:
                    hit = "fav"
                    break
                if ha:
                    hit = "adv"
                    break
            counts[side][lvl][hit] += 1
    rows = []
    for side in ("LONG", "SHORT", "COMBINED"):
        for lvl in FIRST_TOUCH_LEVELS:
            if side == "COMBINED":
                n = counts["LONG"][lvl]["n"] + counts["SHORT"][lvl]["n"]
                fav = counts["LONG"][lvl]["fav"] + counts["SHORT"][lvl]["fav"]
                adv = counts["LONG"][lvl]["adv"] + counts["SHORT"][lvl]["adv"]
                both = counts["LONG"][lvl]["both"] + counts["SHORT"][lvl]["both"]
                none = counts["LONG"][lvl]["none"] + counts["SHORT"][lvl]["none"]
            else:
                n = counts[side][lvl]["n"]
                fav = counts[side][lvl]["fav"]
                adv = counts[side][lvl]["adv"]
                both = counts[side][lvl]["both"]
                none = counts[side][lvl]["none"]
            rows.append(
                {
                    "side": side,
                    "level_pct": lvl,
                    "n": n,
                    "share_favorable_first": fav / n if n else None,
                    "share_adverse_first": adv / n if n else None,
                    "share_both_same_bar": both / n if n else None,
                    "share_none": none / n if n else None,
                }
            )
    return rows
