"""Path / TPSL helpers for confluence entries."""

from __future__ import annotations

from typing import Any

import numpy as np

from orderbook_analyse.fractal_signal_confluence_db import FEE_PCT, MIN_SAMPLE, VERY_SMALL


def sample_flag(n: int) -> str:
    if n < VERY_SMALL:
        return "VERY_SMALL_SAMPLE"
    if n < MIN_SAMPLE:
        return "SMALL_SAMPLE"
    return "OK"


def path_at_entry(
    ei: int,
    epx: float,
    side: str,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_times: np.ndarray,
    horizons: tuple[int, ...],
) -> dict[str, Any]:
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
    return out


def sim_tpsl(
    ei: int,
    epx: float,
    side: str,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_times: np.ndarray,
    tp: float,
    sl: float,
    max_hold: int,
) -> dict[str, Any]:
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
    if i_tp < 0 or (i_sl >= 0 and i_sl <= i_tp):
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


def summarize_rets(rets: list[float], mfes: list[float], maes: list[float], **meta) -> dict[str, Any]:
    x = np.asarray([r for r in rets if r == r], dtype=float)
    n = int(len(x))
    row: dict[str, Any] = {**meta, "n": n, "sample_flag": sample_flag(n)}
    if n == 0:
        return row
    row.update(
        {
            "hit_rate": float(np.mean(x > 0)),
            "mean_dir_ret": float(np.mean(x)),
            "median_dir_ret": float(np.median(x)),
        }
    )
    mf = np.asarray([v for v in mfes if v == v], dtype=float)
    ma = np.asarray([v for v in maes if v == v], dtype=float)
    if len(mf):
        row["median_mfe"] = float(np.median(mf))
        row["mean_mfe"] = float(np.mean(mf))
    if len(ma):
        row["median_mae"] = float(np.median(ma))
        row["mean_mae"] = float(np.mean(ma))
    return row


def summarize_nets(nets: list[float], exits: list[str], holds: list[float], **meta) -> dict[str, Any]:
    nets_a = np.asarray(nets, dtype=float)
    ok = np.isfinite(nets_a)
    nets_a = nets_a[ok]
    exits_a = np.asarray(exits, dtype=object)[ok]
    holds_a = np.asarray(holds, dtype=float)[ok]
    n = int(len(nets_a))
    row: dict[str, Any] = {**meta, "n": n, "sample_flag": sample_flag(n)}
    if n == 0:
        return row
    wins = nets_a[nets_a > 0]
    losses = nets_a[nets_a < 0]
    eq = np.cumsum(nets_a)
    dd = eq - np.maximum.accumulate(eq)
    row.update(
        {
            "expectancy": float(np.mean(nets_a)),
            "median_net": float(np.median(nets_a)),
            "win_rate": float(np.mean(nets_a > 0)),
            "tp_rate": float(np.mean(exits_a == "TP")),
            "sl_rate": float(np.mean(exits_a == "SL")),
            "profit_factor": (
                float(np.sum(wins) / abs(np.sum(losses)))
                if len(wins) and len(losses) and np.sum(losses) != 0
                else None
            ),
            "max_drawdown": float(dd.min()) if len(dd) else None,
            "median_hold_min": float(np.median(holds_a)),
            "cumulative_net": float(np.sum(nets_a)),
        }
    )
    return row


def monotonicity(vals: list[float | None], *, higher_better: bool = True) -> str:
    if any(v is None for v in vals) or len(vals) < 2:
        return "INSUFFICIENT"
    if higher_better:
        # expect increasing: SINGLE < DOUBLE < ...
        if all(vals[i] < vals[i + 1] for i in range(len(vals) - 1)):
            return "MONOTONIC"
        if vals[0] <= vals[-1] and sum(
            1 for i in range(len(vals) - 1) if vals[i] <= vals[i + 1]
        ) >= len(vals) - 2:
            return "MOSTLY_MONOTONIC"
        return "NON_MONOTONIC"
    if all(vals[i] > vals[i + 1] for i in range(len(vals) - 1)):
        return "MONOTONIC"
    if vals[0] >= vals[-1]:
        return "MOSTLY_MONOTONIC"
    return "NON_MONOTONIC"
