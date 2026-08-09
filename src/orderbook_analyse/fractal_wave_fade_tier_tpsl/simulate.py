"""Path precompute + TP/SL resolve for tiered fade signals."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_failure_multitimeframe.outcomes import load_1m
from orderbook_analyse.fractal_wave_fade_tier_tpsl import (
    EVENTS_PATH,
    FEE_PCT,
    MAX_HOLD_MIN,
    MIN_SAMPLE,
    SHORT_H_MIN,
    VERY_SMALL,
)


def sample_flag(n: int) -> str:
    if n < VERY_SMALL:
        return "VERY_SMALL_SAMPLE"
    if n < MIN_SAMPLE:
        return "SMALL_SAMPLE"
    return "OK"


def assign_tier(trend_bucket: str, eff_q: str) -> str:
    tb = str(trend_bucket)
    q = str(eff_q)
    if tb == "TREND_ALIGNED" and q == "Q4":
        return "A"
    if tb == "TREND_ALIGNED" and q in ("Q1", "Q2", "Q3"):
        return "B"
    if tb == "COUNTERTREND" and q == "Q4":
        return "C"
    if tb == "COUNTERTREND" and q in ("Q1", "Q2", "Q3"):
        return "D"
    if tb == "MIXED":
        return "MIXED"
    return "OTHER"


def load_events() -> pd.DataFrame:
    usecols = [
        "symbol",
        "timeframe",
        "wave_i",
        "direction",
        "side",
        "ema_context",
        "trend_bucket",
        "eff_quantile",
        "confirmation_available_at",
        "entry_time",
        "entry_price",
    ]
    df = pd.read_csv(EVENTS_PATH, usecols=usecols)
    df = df[df["timeframe"].isin(("15m", "30m", "1h", "4h"))].copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["confirmation_available_at"] = pd.to_datetime(
        df["confirmation_available_at"], utc=True
    )
    df["tier"] = [
        assign_tier(t, q) for t, q in zip(df["trend_bucket"], df["eff_quantile"])
    ]
    return df.reset_index(drop=True)


def resolve_entry_indices(
    events: pd.DataFrame, open_times: np.ndarray, opens: np.ndarray
) -> pd.DataFrame:
    """Map entry_time to 1m bar index (exact match preferred, else searchsorted)."""
    out = events.copy()
    et = out["entry_time"].to_numpy(dtype="datetime64[ns]")
    # entry_time was set as open_time of first bar after confirmation
    idx = np.searchsorted(open_times, et, side="left")
    n = len(open_times)
    valid = (idx >= 0) & (idx < n)
    # verify open_time match within 1m tolerance when possible
    match = np.zeros(len(out), dtype=bool)
    match[valid] = open_times[idx[valid]] == et[valid]
    # if not exact, keep searchsorted left if equal else already left
    out["entry_i"] = np.where(valid, idx, -1)
    out["entry_valid"] = valid & np.isfinite(out["entry_price"].astype(float))
    # refresh price from opens when matched
    px = np.asarray(out["entry_price"].astype(float).to_numpy(), dtype=np.float64).copy()
    px[match] = np.asarray(opens, dtype=np.float64)[idx[match]]
    out["entry_price"] = px
    return out


def build_paths(
    events: pd.DataFrame,
    *,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_times: np.ndarray,
    max_hold_min: int,
) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    n_c = len(close)
    for ev in events.itertuples(index=False):
        if not bool(getattr(ev, "entry_valid", False)):
            paths.append({"valid": False, "meta": ev})
            continue
        ei = int(ev.entry_i)
        epx = float(ev.entry_price)
        side = str(ev.side)
        if ei < 0 or ei >= n_c - 1 or epx <= 0:
            paths.append({"valid": False, "meta": ev})
            continue
        t_end = open_times[ei] + np.timedelta64(int(max_hold_min), "m")
        end_i = int(np.searchsorted(open_times, t_end, side="right") - 1)
        end_i = min(n_c - 1, max(ei + 1, end_i))
        hh = high[ei + 1 : end_i + 1]
        ll = low[ei + 1 : end_i + 1]
        cc = close[ei + 1 : end_i + 1]
        if hh.size == 0:
            paths.append({"valid": False, "meta": ev})
            continue
        if side == "LONG":
            fav = (hh / epx - 1.0) * 100.0
            adv = (ll / epx - 1.0) * 100.0
            raw = (cc / epx - 1.0) * 100.0
        else:
            fav = (epx - ll) / epx * 100.0
            adv = -((hh - epx) / epx * 100.0)
            raw = -((cc / epx - 1.0) * 100.0)
        hold = ((open_times[ei + 1 : end_i + 1] - open_times[ei]) / np.timedelta64(1, "m")).astype(
            float
        )
        paths.append(
            {
                "valid": True,
                "meta": ev,
                "fav": fav.astype(float),
                "adv": adv.astype(float),
                "raw": raw.astype(float),
                "hold_min": hold,
                "entry_time": pd.Timestamp(ev.entry_time),
                "side": side,
                "tier": str(ev.tier),
            }
        )
    return paths


def _first_hit_idx(arr: np.ndarray, threshold: float, *, adverse: bool) -> int:
    if adverse:
        m = arr <= -threshold
    else:
        m = arr >= threshold
    if not np.any(m):
        return -1
    return int(np.argmax(m))


def prep_path_levels(
    path: dict[str, Any],
    tp_levels: tuple[float, ...],
    sl_levels: tuple[float, ...],
) -> None:
    """Cache first-hit indices for TP/SL levels on a path."""
    if not path["valid"] or path.get("_prepped"):
        return
    path["tp_idx"] = {tp: _first_hit_idx(path["fav"], tp, adverse=False) for tp in tp_levels}
    path["sl_idx"] = {sl: _first_hit_idx(path["adv"], sl, adverse=True) for sl in sl_levels}
    path["_prepped"] = True


def resolve_tpsl(
    path: dict[str, Any],
    *,
    tp_pct: float,
    sl_pct: float,
    policy: str = "SL_FIRST",
) -> dict[str, Any]:
    if not path["valid"]:
        return {
            "exit_type": "INVALID",
            "gross": np.nan,
            "net": np.nan,
            "hold_min": np.nan,
            "ambiguous": False,
        }
    fav, adv, raw, hold = path["fav"], path["adv"], path["raw"], path["hold_min"]
    if path.get("_prepped") and tp_pct in path.get("tp_idx", {}) and sl_pct in path.get("sl_idx", {}):
        i_tp = path["tp_idx"][tp_pct]
        i_sl = path["sl_idx"][sl_pct]
    else:
        i_tp = _first_hit_idx(fav, tp_pct, adverse=False)
        i_sl = _first_hit_idx(adv, sl_pct, adverse=True)

    if i_tp < 0 and i_sl < 0:
        g = float(raw[-1])
        return {
            "exit_type": "TIMEOUT",
            "gross": g,
            "net": g - FEE_PCT,
            "hold_min": float(hold[-1]),
            "ambiguous": False,
        }
    if i_tp < 0:
        return {
            "exit_type": "SL",
            "gross": float(-sl_pct),
            "net": float(-(sl_pct + FEE_PCT)),
            "hold_min": float(hold[i_sl]),
            "ambiguous": False,
        }
    if i_sl < 0:
        return {
            "exit_type": "TP",
            "gross": float(tp_pct),
            "net": float(tp_pct - FEE_PCT),
            "hold_min": float(hold[i_tp]),
            "ambiguous": False,
        }
    if i_tp == i_sl:
        if policy == "TP_FIRST":
            return {
                "exit_type": "TP",
                "gross": float(tp_pct),
                "net": float(tp_pct - FEE_PCT),
                "hold_min": float(hold[i_tp]),
                "ambiguous": True,
            }
        return {
            "exit_type": "SL",
            "gross": float(-sl_pct),
            "net": float(-(sl_pct + FEE_PCT)),
            "hold_min": float(hold[i_sl]),
            "ambiguous": True,
        }
    if i_tp < i_sl:
        return {
            "exit_type": "TP",
            "gross": float(tp_pct),
            "net": float(tp_pct - FEE_PCT),
            "hold_min": float(hold[i_tp]),
            "ambiguous": False,
        }
    return {
        "exit_type": "SL",
        "gross": float(-sl_pct),
        "net": float(-(sl_pct + FEE_PCT)),
        "hold_min": float(hold[i_sl]),
        "ambiguous": False,
    }


def summarize_trades(nets: np.ndarray, exits: np.ndarray, holds: np.ndarray, **meta) -> dict[str, Any]:
    n = int(len(nets))
    row: dict[str, Any] = {**meta, "n": n, "sample_flag": sample_flag(n)}
    if n == 0:
        return row
    tp_n = int((exits == "TP").sum())
    sl_n = int((exits == "SL").sum())
    to_n = int((exits == "TIMEOUT").sum())
    row.update(
        {
            "tp_count": tp_n,
            "sl_count": sl_n,
            "timeout_count": to_n,
            "tp_rate": tp_n / n,
            "sl_rate": sl_n / n,
            "timeout_rate": to_n / n,
            "mean_net": float(np.mean(nets)),
            "median_net": float(np.median(nets)),
            "expectancy": float(np.mean(nets)),
            "win_rate": float(np.mean(nets > 0)),
            "loss_rate": float(np.mean(nets < 0)),
            "worst_trade": float(np.min(nets)),
            "best_trade": float(np.max(nets)),
            "q05_net": float(np.quantile(nets, 0.05)),
            "q95_net": float(np.quantile(nets, 0.95)),
            "cumulative_net": float(np.sum(nets)),
            "mean_hold_min": float(np.mean(holds)),
            "median_hold_min": float(np.median(holds)),
        }
    )
    wins = nets[nets > 0]
    losses = nets[nets < 0]
    row["avg_winner"] = float(np.mean(wins)) if len(wins) else None
    row["avg_loser"] = float(np.mean(losses)) if len(losses) else None
    row["payoff_ratio"] = (
        float(np.mean(wins) / abs(np.mean(losses)))
        if len(wins) and len(losses) and np.mean(losses) != 0
        else None
    )
    row["profit_factor"] = (
        float(np.sum(wins) / abs(np.sum(losses)))
        if len(wins) and len(losses) and np.sum(losses) != 0
        else None
    )
    eq = np.cumsum(nets)
    dd = eq - np.maximum.accumulate(eq)
    row["max_drawdown"] = float(dd.min()) if len(dd) else None
    row["recovery_factor"] = (
        float(row["cumulative_net"] / abs(row["max_drawdown"]))
        if row["max_drawdown"] is not None and row["max_drawdown"] < 0
        else None
    )
    row["return_over_dd"] = row["recovery_factor"]
    # consecutive losses
    max_l = cur = 0
    for x in nets:
        if x < 0:
            cur += 1
            max_l = max(max_l, cur)
        else:
            cur = 0
    row["max_consecutive_losses"] = int(max_l)
    return row


def run_combo_on_paths(
    paths: list[dict],
    *,
    tp: float,
    sl: float,
    policy: str = "SL_FIRST",
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    nets, exits, holds = [], [], []
    for p in paths:
        if not p["valid"]:
            continue
        sim = resolve_tpsl(p, tp_pct=tp, sl_pct=sl, policy=policy)
        if sim["exit_type"] == "INVALID":
            continue
        nets.append(sim["net"])
        exits.append(sim["exit_type"])
        holds.append(sim["hold_min"])
    return (
        np.asarray(nets, dtype=float),
        np.asarray(exits, dtype=object),
        np.asarray(holds, dtype=float),
    )


def mfe_mae_summary(paths: list[dict], **meta) -> dict[str, Any]:
    mfes, maes = [], []
    for p in paths:
        if not p["valid"]:
            continue
        mfes.append(float(np.max(p["fav"])))
        maes.append(float(np.min(p["adv"])))
    row = {**meta, "n": len(mfes), "sample_flag": sample_flag(len(mfes))}
    if not mfes:
        return row
    for name, arr in (("mfe", np.asarray(mfes)), ("mae", np.asarray(maes))):
        for q, label in (
            (0.10, "q10"),
            (0.25, "q25"),
            (0.50, "median"),
            (0.75, "q75"),
            (0.90, "q90"),
            (0.95, "q95"),
        ):
            row[f"{name}_{label}"] = float(np.quantile(arr, q))
    return row


def reachability(paths: list[dict], level: float, **meta) -> dict[str, Any]:
    times = []
    hits = 0
    n = 0
    for p in paths:
        if not p["valid"]:
            continue
        n += 1
        fav, hold = p["fav"], p["hold_min"]
        m = fav >= level
        if np.any(m):
            hits += 1
            i = int(np.argmax(m))
            times.append(float(hold[i]))
    row = {
        **meta,
        "level_pct": level,
        "n": n,
        "reach_rate": hits / n if n else None,
        "sample_flag": sample_flag(n),
    }
    if times:
        t = np.asarray(times)
        row["median_time_to_touch_min"] = float(np.median(t))
        row["q25_time_to_touch_min"] = float(np.quantile(t, 0.25))
        row["q75_time_to_touch_min"] = float(np.quantile(t, 0.75))
    return row


def large_move_success(
    paths: list[dict],
    *,
    target: float,
    max_adverse: float,
    **meta,
) -> dict[str, Any]:
    """Success if fav hits target before adv hits -max_adverse."""
    n = ok = 0
    for p in paths:
        if not p["valid"]:
            continue
        n += 1
        fav, adv = p["fav"], p["adv"]
        for f, a in zip(fav, adv):
            ht, ha = f >= target, a <= -max_adverse
            if ht and ha:
                break  # ambiguous same bar -> fail under conservative
            if ha:
                break
            if ht:
                ok += 1
                break
    return {
        **meta,
        "target_pct": target,
        "max_allowed_adverse_pct": max_adverse,
        "n": n,
        "success_rate": ok / n if n else None,
        "sample_flag": sample_flag(n),
    }
