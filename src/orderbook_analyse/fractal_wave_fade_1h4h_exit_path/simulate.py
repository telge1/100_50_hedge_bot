"""Path build, single-TP and scale-out simulation for Tier-A exit research."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_1h4h_exit_path import (
    ADV_LEVELS,
    EVENTS_PATH,
    FAV_LEVELS,
    FEE_PCT,
    MIN_SAMPLE,
    VERY_SMALL,
)
from orderbook_analyse.fractal_wave_fade_tier_tpsl.simulate import (
    assign_tier,
    resolve_entry_indices,
)


def sample_flag(n: int) -> str:
    if n < VERY_SMALL:
        return "VERY_SMALL_SAMPLE"
    if n < MIN_SAMPLE:
        return "SMALL_SAMPLE"
    return "OK"


def load_tier_a_events() -> pd.DataFrame:
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
    df = df[df["timeframe"].isin(("1h", "4h"))].copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["confirmation_available_at"] = pd.to_datetime(
        df["confirmation_available_at"], utc=True
    )
    df["tier"] = [
        assign_tier(t, q) for t, q in zip(df["trend_bucket"], df["eff_quantile"])
    ]
    df = df[df["tier"] == "A"].reset_index(drop=True)
    return df


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
            continue
        ei = int(ev.entry_i)
        epx = float(ev.entry_price)
        side = str(ev.side)
        if ei < 0 or ei >= n_c - 1 or epx <= 0:
            continue
        t_end = open_times[ei] + np.timedelta64(int(max_hold_min), "m")
        end_i = int(np.searchsorted(open_times, t_end, side="right") - 1)
        end_i = min(n_c - 1, max(ei + 1, end_i))
        hh = high[ei + 1 : end_i + 1]
        ll = low[ei + 1 : end_i + 1]
        cc = close[ei + 1 : end_i + 1]
        if hh.size == 0:
            continue
        if side == "LONG":
            fav = (hh / epx - 1.0) * 100.0
            adv = (ll / epx - 1.0) * 100.0
            raw = (cc / epx - 1.0) * 100.0
        else:
            fav = (epx - ll) / epx * 100.0
            adv = -((hh - epx) / epx * 100.0)
            raw = -((cc / epx - 1.0) * 100.0)
        hold = (
            (open_times[ei + 1 : end_i + 1] - open_times[ei]) / np.timedelta64(1, "m")
        ).astype(float)
        paths.append(
            {
                "valid": True,
                "symbol": str(ev.symbol),
                "timeframe": str(ev.timeframe),
                "side": side,
                "tier": "A",
                "entry_time": pd.Timestamp(ev.entry_time),
                "fav": fav.astype(np.float64),
                "adv": adv.astype(np.float64),
                "raw": raw.astype(np.float64),
                "hold_min": hold,
            }
        )
    return paths


def _first_hit(arr: np.ndarray, thr: float, *, adverse: bool) -> int:
    m = arr <= -thr if adverse else arr >= thr
    if not np.any(m):
        return -1
    return int(np.argmax(m))


def path_metrics(path: dict[str, Any]) -> dict[str, Any]:
    fav, adv, hold = path["fav"], path["adv"], path["hold_min"]
    mfe = float(np.max(fav))
    mae = float(np.min(adv))
    i_mfe = int(np.argmax(fav))
    i_mae = int(np.argmin(adv))
    row = {
        "symbol": path["symbol"],
        "timeframe": path["timeframe"],
        "side": path["side"],
        "entry_time": path["entry_time"].isoformat(),
        "mfe": mfe,
        "mae": mae,
        "time_to_mfe_min": float(hold[i_mfe]),
        "time_to_mae_min": float(hold[i_mae]),
        "path_len_min": float(hold[-1]),
        "final_raw": float(path["raw"][-1]),
    }
    for lvl in FAV_LEVELS:
        i = _first_hit(fav, lvl, adverse=False)
        row[f"t_fav_{lvl:g}"] = float(hold[i]) if i >= 0 else None
        row[f"hit_fav_{lvl:g}"] = i >= 0
    for lvl in ADV_LEVELS:
        i = _first_hit(adv, lvl, adverse=True)
        row[f"t_adv_{lvl:g}"] = float(hold[i]) if i >= 0 else None
        row[f"hit_adv_{lvl:g}"] = i >= 0
    return row


def target_before_adverse(path: dict[str, Any], target: float, adverse: float) -> bool:
    """True if fav hits target before adv hits -adverse. Same-bar => fail (SL_FIRST)."""
    fav, adv = path["fav"], path["adv"]
    for f, a in zip(fav, adv):
        ht, ha = f >= target, a <= -adverse
        if ht and ha:
            return False
        if ha:
            return False
        if ht:
            return True
    return False


def simulate_single_tpsl(
    path: dict[str, Any],
    *,
    tp_pct: float,
    sl_pct: float,
    policy: str = "SL_FIRST",
) -> dict[str, Any]:
    fav, adv, raw, hold = path["fav"], path["adv"], path["raw"], path["hold_min"]
    i_tp = _first_hit(fav, tp_pct, adverse=False)
    i_sl = _first_hit(adv, sl_pct, adverse=True)
    mfe = float(np.max(fav))

    def pack(exit_type: str, gross: float, idx: int, ambiguous: bool = False) -> dict:
        net = gross - FEE_PCT
        return {
            "exit_type": exit_type,
            "gross": gross,
            "net": net,
            "hold_min": float(hold[idx]),
            "ambiguous": ambiguous,
            "mfe": mfe,
            "capture_ratio": (gross / mfe) if mfe > 0 else None,
            "time_tp1_min": float(hold[i_tp]) if i_tp >= 0 and exit_type == "TP" else (
                float(hold[i_tp]) if i_tp >= 0 else None
            ),
        }

    if i_tp < 0 and i_sl < 0:
        g = float(raw[-1])
        return pack("TIMEOUT", g, -1)
    if i_tp < 0:
        return pack("SL", float(-sl_pct), i_sl)
    if i_sl < 0:
        return pack("TP", float(tp_pct), i_tp)
    if i_tp == i_sl:
        if policy == "TP_FIRST":
            return pack("TP", float(tp_pct), i_tp, True)
        return pack("SL", float(-sl_pct), i_sl, True)
    if i_tp < i_sl:
        return pack("TP", float(tp_pct), i_tp)
    return pack("SL", float(-sl_pct), i_sl)


def simulate_scaleout(
    path: dict[str, Any],
    *,
    legs: tuple[tuple[float, float | None], ...],
    sl_pct: float,
    be_after_first_tp: bool,
    policy: str = "SL_FIRST",
) -> dict[str, Any]:
    """
    Walk 1m path; close fixed weights at TP legs; remaining can hit SL / BE / timeout.

    Fee: each closed weight w contributes w * (gross - FEE_PCT).
    Multiple fixed TPs may fill on the same bar (ascending TP order) if SL not also hit
    (SL_FIRST: same-bar SL blocks TP fills that bar).
    """
    fav, adv, raw, hold = path["fav"], path["adv"], path["raw"], path["hold_min"]
    mfe = float(np.max(fav))
    n = len(fav)
    remaining_legs = list(legs)  # (w, tp|None)
    active_sl = float(sl_pct)
    first_tp_done = False
    realized_w = 0.0
    realized_gross_sum = 0.0
    realized_net_sum = 0.0
    time_tp1 = None
    last_idx = n - 1
    exit_parts: list[str] = []

    def close_weight(w: float, gross: float, etype: str, idx: int) -> None:
        nonlocal realized_w, realized_gross_sum, realized_net_sum, time_tp1, last_idx
        realized_w += w
        realized_gross_sum += w * gross
        realized_net_sum += w * (gross - FEE_PCT)
        exit_parts.append(etype)
        last_idx = idx
        if etype.startswith("TP") and time_tp1 is None:
            time_tp1 = float(hold[idx])

    for i in range(n):
        if not remaining_legs:
            break
        f, a = float(fav[i]), float(adv[i])
        rem_w = sum(w for w, _ in remaining_legs)
        hit_sl = a <= -active_sl
        hittable = [(w, tp) for (w, tp) in remaining_legs if tp is not None and f >= tp]
        hit_tp = len(hittable) > 0

        if hit_sl and hit_tp and policy != "TP_FIRST":
            hit_tp = False
        if hit_sl and hit_tp and policy == "TP_FIRST":
            hit_sl = False

        if hit_sl:
            g = 0.0 if abs(active_sl) < 1e-12 else float(-active_sl)
            et = "BE" if abs(active_sl) < 1e-12 else "SL"
            close_weight(rem_w, g, et, i)
            remaining_legs = []
            break

        if hit_tp:
            # fill all fixed TPs reached this bar, ascending
            hittable_sorted = sorted(hittable, key=lambda x: x[1])  # type: ignore[arg-type]
            filled_tps = {tp for _, tp in hittable_sorted}
            new_legs = []
            for w, tp in remaining_legs:
                if tp is not None and tp in filled_tps:
                    close_weight(w, float(tp), f"TP{tp:g}", i)
                    if not first_tp_done:
                        first_tp_done = True
                        if be_after_first_tp:
                            active_sl = 0.0
                    continue
                new_legs.append((w, tp))
            remaining_legs = new_legs
            # if BE armed mid-bar after first TP, remaining could also hit BE same bar
            if remaining_legs and be_after_first_tp and first_tp_done and abs(active_sl) < 1e-12:
                if a <= 0:
                    rem_w = sum(w for w, _ in remaining_legs)
                    close_weight(rem_w, 0.0, "BE", i)
                    remaining_legs = []
                    break

    if remaining_legs:
        rem_w = sum(w for w, _ in remaining_legs)
        close_weight(rem_w, float(raw[-1]), "TIMEOUT", n - 1)

    return {
        "exit_type": "+".join(exit_parts) if exit_parts else "NONE",
        "gross": float(realized_gross_sum),
        "net": float(realized_net_sum),
        "hold_min": float(hold[last_idx]),
        "time_tp1_min": time_tp1,
        "mfe": mfe,
        "capture_ratio": (realized_gross_sum / mfe) if mfe > 0 else None,
        "weight_closed": float(realized_w),
        "first_tp_done": first_tp_done,
        "be_armed": bool(be_after_first_tp and first_tp_done),
    }


def summarize_trade_list(trades: list[dict[str, Any]], **meta) -> dict[str, Any]:
    n = len(trades)
    row: dict[str, Any] = {**meta, "n": n, "sample_flag": sample_flag(n)}
    if n == 0:
        return row
    nets = np.asarray([t["net"] for t in trades], dtype=float)
    gross = np.asarray([t["gross"] for t in trades], dtype=float)
    holds = np.asarray([t["hold_min"] for t in trades], dtype=float)
    caps = np.asarray(
        [t["capture_ratio"] for t in trades if t.get("capture_ratio") is not None],
        dtype=float,
    )
    tp1s = np.asarray(
        [t["time_tp1_min"] for t in trades if t.get("time_tp1_min") is not None],
        dtype=float,
    )
    wins = nets[nets > 0]
    losses = nets[nets < 0]
    row.update(
        {
            "mean_net": float(np.mean(nets)),
            "median_net": float(np.median(nets)),
            "expectancy": float(np.mean(nets)),
            "win_rate": float(np.mean(nets > 0)),
            "avg_winner": float(np.mean(wins)) if len(wins) else None,
            "avg_loser": float(np.mean(losses)) if len(losses) else None,
            "payoff_ratio": (
                float(np.mean(wins) / abs(np.mean(losses)))
                if len(wins) and len(losses) and np.mean(losses) != 0
                else None
            ),
            "profit_factor": (
                float(np.sum(wins) / abs(np.sum(losses)))
                if len(wins) and len(losses) and np.sum(losses) != 0
                else None
            ),
            "cumulative_net": float(np.sum(nets)),
            "worst_trade": float(np.min(nets)),
            "best_trade": float(np.max(nets)),
            "q05_net": float(np.quantile(nets, 0.05)),
            "median_hold_min": float(np.median(holds)),
            "mean_gross": float(np.mean(gross)),
        }
    )
    eq = np.cumsum(nets)
    dd = eq - np.maximum.accumulate(eq)
    row["max_drawdown"] = float(dd.min()) if len(dd) else None
    row["recovery_factor"] = (
        float(row["cumulative_net"] / abs(row["max_drawdown"]))
        if row["max_drawdown"] is not None and row["max_drawdown"] < 0
        else None
    )
    max_l = cur = 0
    for x in nets:
        if x < 0:
            cur += 1
            max_l = max(max_l, cur)
        else:
            cur = 0
    row["max_consecutive_losses"] = int(max_l)
    if len(caps):
        row["median_capture_ratio"] = float(np.median(caps))
        row["q25_capture_ratio"] = float(np.quantile(caps, 0.25))
        row["q75_capture_ratio"] = float(np.quantile(caps, 0.75))
    if len(tp1s):
        row["median_time_tp1_min"] = float(np.median(tp1s))
    row["median_time_final_exit_min"] = float(np.median(holds))
    return row


def giveback_for_trades(trades: list[dict[str, Any]], mfe_min: float, **meta) -> dict[str, Any]:
    subset = [t for t in trades if (t.get("mfe") or 0) >= mfe_min]
    row: dict[str, Any] = {**meta, "mfe_min": mfe_min, "n": len(subset)}
    if not subset:
        return row
    mfe = np.asarray([t["mfe"] for t in subset], dtype=float)
    gross = np.asarray([t["gross"] for t in subset], dtype=float)
    giveback = mfe - gross  # positive => under-capture
    caps = np.asarray(
        [t["capture_ratio"] for t in subset if t.get("capture_ratio") is not None],
        dtype=float,
    )
    row.update(
        {
            "mean_mfe": float(np.mean(mfe)),
            "mean_realized_gross": float(np.mean(gross)),
            "median_realized_gross": float(np.median(gross)),
            "mean_giveback": float(np.mean(giveback)),
            "median_giveback": float(np.median(giveback)),
            "median_capture_ratio": float(np.median(caps)) if len(caps) else None,
            "q25_capture_ratio": float(np.quantile(caps, 0.25)) if len(caps) else None,
            "q75_capture_ratio": float(np.quantile(caps, 0.75)) if len(caps) else None,
        }
    )
    return row


__all__ = [
    "load_tier_a_events",
    "resolve_entry_indices",
    "build_paths",
    "path_metrics",
    "target_before_adverse",
    "simulate_single_tpsl",
    "simulate_scaleout",
    "summarize_trade_list",
    "giveback_for_trades",
    "sample_flag",
    "FAV_LEVELS",
    "ADV_LEVELS",
]
