"""Causal path simulation for P0 / P5A/B/C dynamic cluster upgrades."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_dynamic_cluster_upgrade_db import (
    FEE_PCT,
    MAX_HOLD_BY_TF,
    MIN_SAMPLE,
    PROFIT_BUCKETS,
    TIME_BUCKETS,
    TPSL_BY_TF,
    TPSL_EXTRA_4H,
    VERY_SMALL,
)
from orderbook_analyse.fractal_signal_confluence_db import TF_RANK
from orderbook_analyse.fractal_signal_confluence_db.cluster import pair_window


def sample_flag(n: int) -> str:
    if n < VERY_SMALL:
        return "VERY_SMALL_SAMPLE"
    if n < MIN_SAMPLE:
        return "SMALL_SAMPLE"
    return "OK"


def profit_bucket(x: float) -> str:
    for name, lo, hi in PROFIT_BUCKETS:
        if lo <= x < hi:
            return name
    return "gt4"


def time_bucket(mins: float) -> str:
    for name, lo, hi in TIME_BUCKETS:
        if lo <= mins < hi:
            return name
    return "gt4h"


def tpsl_for_tf(tf: str, *, extra_4h: bool = False) -> tuple[float, float]:
    if tf == "4h" and extra_4h:
        return float(TPSL_EXTRA_4H[0]), float(TPSL_EXTRA_4H[1])
    tp, sl = TPSL_BY_TF[tf]
    return float(tp), float(sl)


def apply_upgrade_plan(
    policy: str,
    old_tp: float,
    old_sl: float,
    new_tf: str,
    *,
    extra_4h: bool = False,
) -> tuple[float, float]:
    ntp, nsl = tpsl_for_tf(new_tf, extra_4h=extra_4h)
    if policy == "P5A":
        return ntp, nsl
    if policy == "P5B":
        return ntp, old_sl
    if policy == "P5C":
        return ntp, min(old_sl, nsl)
    return old_tp, old_sl


def _dir_ret_open(side: str, epx: float, px: float) -> float:
    if side == "LONG":
        return (px / epx - 1.0) * 100.0
    return (epx - px) / epx * 100.0


def _mfe_mae_slice(
    side: str,
    epx: float,
    high: np.ndarray,
    low: np.ndarray,
    a: int,
    b: int,
) -> tuple[float, float]:
    if b < a:
        return 0.0, 0.0
    hh = high[a : b + 1]
    ll = low[a : b + 1]
    if hh.size == 0:
        return 0.0, 0.0
    if side == "LONG":
        mfe = (float(np.max(hh)) / epx - 1.0) * 100.0
        mae = (float(np.min(ll)) / epx - 1.0) * 100.0
    else:
        mfe = (epx - float(np.min(ll))) / epx * 100.0
        mae = -((float(np.max(hh)) - epx) / epx * 100.0)
    return float(mfe), float(mae)


def _hold_end_i(ei: int, open_times: np.ndarray, max_hold_min: int, n: int) -> int:
    t_end = open_times[ei] + np.timedelta64(int(max_hold_min), "m")
    return min(n - 1, max(ei + 1, int(np.searchsorted(open_times, t_end, side="right") - 1)))


def _bar_exit(
    side: str,
    epx: float,
    h: float,
    l: float,
    c: float,
    tp: float,
    sl: float,
) -> tuple[str | None, float | None]:
    if side == "LONG":
        fav_h = (h / epx - 1.0) * 100.0
        adv_l = (l / epx - 1.0) * 100.0
        hit_tp = fav_h >= tp
        hit_sl = adv_l <= -sl
        if hit_tp and hit_sl:
            return "SL", -sl  # SL_FIRST
        if hit_sl:
            return "SL", -sl
        if hit_tp:
            return "TP", tp
        return None, None
    # SHORT
    fav_l = (epx - l) / epx * 100.0
    adv_h = -((h - epx) / epx * 100.0)
    hit_tp = fav_l >= tp
    hit_sl = adv_h <= -sl
    if hit_tp and hit_sl:
        return "SL", -sl
    if hit_sl:
        return "SL", -sl
    if hit_tp:
        return "TP", tp
    return None, None


def collect_upgrade_candidates(cluster_rows: pd.DataFrame, first_tf: str) -> list[dict[str, Any]]:
    """Same-side higher-TF signals after first, sorted by entry_i."""
    out = []
    first_rank = TF_RANK[first_tf]
    for _, row in cluster_rows.iloc[1:].iterrows():
        tf = str(row["signal_tf"])
        if TF_RANK[tf] <= first_rank:
            continue
        if not bool(row.get("entry_valid", False)):
            continue
        out.append(
            {
                "tf": tf,
                "entry_i": int(row["entry_i"]),
                "entry_time": pd.Timestamp(row["entry_time"]),
                "confirmation": pd.Timestamp(row["confirmation_available_at"]),
            }
        )
    out.sort(key=lambda x: (x["entry_i"], TF_RANK[x["tf"]]))
    return out


def collect_conflict_candidates(
    entry_row: pd.Series,
    sig_times: np.ndarray,
    sig_sides: np.ndarray,
    sig_tfs: np.ndarray,
    sig_entry_i: np.ndarray,
    sig_entry_valid: np.ndarray,
    sig_entry_time: np.ndarray,
) -> list[dict[str, Any]]:
    """Higher-TF opposite signals after entry, within pair window of entry confirmation."""
    side = str(entry_row["side"])
    entry_tf = str(entry_row["signal_tf"])
    entry_conf = np.datetime64(pd.Timestamp(entry_row["confirmation_available_at"]).to_datetime64())
    entry_i = int(entry_row["entry_i"])
    start = int(np.searchsorted(sig_times, entry_conf, side="right"))
    max_look = entry_conf + np.timedelta64(480, "m")
    end = int(np.searchsorted(sig_times, max_look, side="right"))
    out = []
    for j in range(start, end):
        if not bool(sig_entry_valid[j]):
            continue
        if str(sig_sides[j]) == side:
            continue
        tf = str(sig_tfs[j])
        if TF_RANK[tf] <= TF_RANK[entry_tf]:
            continue
        ci = int(sig_entry_i[j])
        if ci <= entry_i:
            continue
        dt = float((sig_times[j] - entry_conf) / np.timedelta64(1, "m"))
        if dt > pair_window(entry_tf, tf):
            continue
        out.append(
            {
                "tf": tf,
                "entry_i": ci,
                "entry_time": pd.Timestamp(sig_entry_time[j]),
                "confirmation": pd.Timestamp(sig_times[j]),
                "side": str(sig_sides[j]),
            }
        )
    out.sort(key=lambda x: x["entry_i"])
    return out


def _scan_exit_range(
    side: str,
    epx: float,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_times: np.ndarray,
    start_i: int,
    end_i: int,
    tp: float,
    sl: float,
    ei: int,
) -> tuple[str | None, float | None, int | None]:
    """SL_FIRST scan over [start_i, end_i]. Returns (exit_type, gross, exit_i)."""
    if end_i < start_i:
        return None, None, None
    hh = high[start_i : end_i + 1]
    ll = low[start_i : end_i + 1]
    if hh.size == 0:
        return None, None, None
    if side == "LONG":
        fav = (hh / epx - 1.0) * 100.0
        adv = (ll / epx - 1.0) * 100.0
    else:
        fav = (epx - ll) / epx * 100.0
        adv = -((hh - epx) / epx * 100.0)
    hit_tp = fav >= tp
    hit_sl = adv <= -sl
    i_tp = int(np.argmax(hit_tp)) if np.any(hit_tp) else -1
    if not np.any(hit_tp):
        i_tp = -1
    i_sl = int(np.argmax(hit_sl)) if np.any(hit_sl) else -1
    if not np.any(hit_sl):
        i_sl = -1
    if i_tp < 0 and i_sl < 0:
        return None, None, None
    if i_tp < 0 or (i_sl >= 0 and i_sl <= i_tp):
        return "SL", float(-sl), start_i + i_sl
    return "TP", float(tp), start_i + i_tp


def simulate_trade(
    *,
    side: str,
    ei: int,
    epx: float,
    first_tf: str,
    policy: str,
    upgrades: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    conflict_mode: str | None,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    opens: np.ndarray,
    open_times: np.ndarray,
    extra_4h: bool = False,
) -> dict[str, Any]:
    """Simulate one cluster trade under P0 / P5* with optional conflict overlay."""
    n = len(close)
    if ei < 0 or epx <= 0 or ei >= n - 1:
        return {"exit_type": "INVALID", "net": np.nan, "gross": np.nan}

    tp, sl = tpsl_for_tf(first_tf, extra_4h=False)
    original_tp, original_sl = tp, sl
    plan_tf = first_tf
    max_hold = MAX_HOLD_BY_TF[first_tf]
    end_i = _hold_end_i(ei, open_times, max_hold, n)

    allow_upgrades = policy != "P0"
    freeze_upgrades = False
    upgrade_log: list[dict[str, Any]] = []
    upgrade_tfs: list[str] = []
    last_signal_i = ei
    first_upgrade_i: int | None = None
    opposite_after_upgrade = False
    conflict_hit = False
    conflict_tf = None

    pending = [u for u in upgrades if u["entry_i"] > ei] if allow_upgrades else []
    confs = [c for c in conflicts if c["entry_i"] > ei]

    # event bars: upgrades + conflicts within hold window (extendable)
    event_is = sorted(
        {
            u["entry_i"]
            for u in pending
            if u["entry_i"] <= end_i
        }
        | {c["entry_i"] for c in confs if c["entry_i"] <= end_i}
    )

    cursor = ei + 1
    exit_type = "TIMEOUT"
    gross = _dir_ret_open(side, epx, float(close[end_i]))
    exit_i = end_i
    u_ptr = c_ptr = 0

    def _apply_upgrades_at(bar_i: int) -> None:
        nonlocal tp, sl, plan_tf, max_hold, end_i, last_signal_i, first_upgrade_i, freeze_upgrades, u_ptr
        while u_ptr < len(pending) and pending[u_ptr]["entry_i"] == bar_i:
            u = pending[u_ptr]
            u_ptr += 1
            if freeze_upgrades or not allow_upgrades:
                continue
            if TF_RANK[u["tf"]] <= TF_RANK[plan_tf]:
                continue
            open_px = float(opens[bar_i])
            profit_u = _dir_ret_open(side, epx, open_px)
            mfe_b, mae_b = _mfe_mae_slice(side, epx, high, low, ei + 1, bar_i - 1)
            old_tp, old_sl = tp, sl
            tp, sl = apply_upgrade_plan(policy, tp, sl, u["tf"], extra_4h=extra_4h)
            plan_tf = u["tf"]
            max_hold = max(max_hold, MAX_HOLD_BY_TF[u["tf"]])
            end_i = max(end_i, _hold_end_i(ei, open_times, max_hold, n))
            elapsed = float((open_times[bar_i] - open_times[ei]) / np.timedelta64(1, "m"))
            dt_prev = float((open_times[bar_i] - open_times[last_signal_i]) / np.timedelta64(1, "m"))
            near = []
            if original_tp > 0:
                frac = profit_u / original_tp
                if frac >= 0.5:
                    near.append("ge50")
                if frac >= 0.75:
                    near.append("ge75")
                if frac >= 0.9:
                    near.append("ge90")
            upgrade_log.append(
                {
                    "tf": u["tf"],
                    "bar_i": bar_i,
                    "profit_at_upgrade": profit_u,
                    "mfe_before": mfe_b,
                    "mae_before": mae_b,
                    "elapsed_min": elapsed,
                    "dt_since_last_signal_min": dt_prev,
                    "time_bucket": time_bucket(dt_prev),
                    "profit_bucket": profit_bucket(profit_u),
                    "original_tp": original_tp,
                    "new_tp": tp,
                    "original_sl": original_sl,
                    "old_sl": old_sl,
                    "new_sl": sl,
                    "near_tp_flags": near,
                }
            )
            upgrade_tfs.append(u["tf"])
            if first_upgrade_i is None:
                first_upgrade_i = bar_i
            last_signal_i = bar_i

    def _apply_conflicts_at(bar_i: int) -> tuple[bool, str | None, float | None, int | None]:
        nonlocal freeze_upgrades, opposite_after_upgrade, conflict_hit, conflict_tf, c_ptr
        while c_ptr < len(confs) and confs[c_ptr]["entry_i"] == bar_i:
            c = confs[c_ptr]
            c_ptr += 1
            if first_upgrade_i is not None and upgrade_tfs and TF_RANK[c["tf"]] >= TF_RANK[upgrade_tfs[-1]]:
                opposite_after_upgrade = True
            if TF_RANK[c["tf"]] <= TF_RANK[plan_tf]:
                continue
            conflict_hit = True
            conflict_tf = c["tf"]
            if conflict_mode is None:
                continue
            if conflict_mode == "C1":
                return True, "CONFLICT_EXIT", _dir_ret_open(side, epx, float(opens[bar_i])), bar_i
            if conflict_mode == "C2":
                freeze_upgrades = True
            if conflict_mode == "C3":
                return True, "HIGHER_TF_DOMINANCE_EXIT", _dir_ret_open(side, epx, float(opens[bar_i])), bar_i
        return False, None, None, None

    # rebuild event list dynamically as end_i extends
    processed_events: set[int] = set()
    done = False
    while not done:
        # refresh events up to current end_i
        event_is = sorted(
            (
                {u["entry_i"] for u in pending if cursor <= u["entry_i"] <= end_i}
                | {c["entry_i"] for c in confs if cursor <= c["entry_i"] <= end_i}
            )
            - processed_events
        )
        if not event_is:
            # final scan to end_i
            et, eg, exi = _scan_exit_range(
                side, epx, high, low, close, open_times, cursor, end_i, tp, sl, ei
            )
            if et is not None:
                exit_type, gross, exit_i = et, float(eg), int(exi)
            else:
                exit_type = "TIMEOUT"
                exit_i = end_i
                gross = _dir_ret_open(side, epx, float(close[end_i]))
            done = True
            break

        bar_i = event_is[0]
        processed_events.add(bar_i)
        # scan exits before this event bar
        if cursor <= bar_i - 1:
            et, eg, exi = _scan_exit_range(
                side, epx, high, low, close, open_times, cursor, bar_i - 1, tp, sl, ei
            )
            if et is not None:
                exit_type, gross, exit_i = et, float(eg), int(exi)
                done = True
                break

        # at event bar open: upgrades then conflicts, then H/L exit
        _apply_upgrades_at(bar_i)
        hit, et, eg, exi = _apply_conflicts_at(bar_i)
        if hit:
            exit_type, gross, exit_i = et, float(eg), int(exi)
            done = True
            break

        et, eg, exi = _scan_exit_range(
            side, epx, high, low, close, open_times, bar_i, bar_i, tp, sl, ei
        )
        if et is not None:
            exit_type, gross, exit_i = et, float(eg), int(exi)
            done = True
            break
        cursor = bar_i + 1
        if cursor > end_i:
            exit_type = "TIMEOUT"
            exit_i = end_i
            gross = _dir_ret_open(side, epx, float(close[end_i]))
            done = True

    # post-upgrade giveback / path
    givebacks = []
    mfe_before_first = mae_before_first = mfe_after = mae_after = None
    if first_upgrade_i is not None:
        mfe_before_first, mae_before_first = _mfe_mae_slice(
            side, epx, high, low, ei + 1, first_upgrade_i - 1
        )
        mfe_after, mae_after = _mfe_mae_slice(side, epx, high, low, first_upgrade_i, exit_i)
        mfe_post, _ = _mfe_mae_slice(side, epx, high, low, first_upgrade_i, exit_i)
        for ug in upgrade_log:
            pau = float(ug["profit_at_upgrade"])
            giveback = max(0.0, pau - float(gross))
            givebacks.append(
                {
                    **ug,
                    "max_profit_after_upgrade": float(mfe_post),
                    "realized_gross": float(gross),
                    "giveback_from_upgrade_profit": giveback,
                }
            )

    seq = first_tf
    for t in upgrade_tfs:
        seq = f"{seq}->{t}"

    hold_min = float((open_times[exit_i] - open_times[ei]) / np.timedelta64(1, "m"))
    net = float(gross) - FEE_PCT
    return {
        "exit_type": exit_type,
        "gross": float(gross),
        "net": net,
        "hold_min": hold_min,
        "plan_tf_final": plan_tf,
        "n_upgrades": len(upgrade_tfs),
        "upgrade_sequence": seq,
        "highest_tf_reached": plan_tf if policy != "P0" else first_tf,
        "upgrade_log": givebacks if givebacks else upgrade_log,
        "mfe_before_first_upgrade": mfe_before_first,
        "mae_before_first_upgrade": mae_before_first,
        "mfe_after_upgrade": mfe_after,
        "mae_after_upgrade": mae_after,
        "conflict_hit": conflict_hit,
        "conflict_tf": conflict_tf,
        "opposite_after_upgrade": opposite_after_upgrade,
        "original_tp": original_tp,
        "original_sl": original_sl,
        "final_tp": tp,
        "final_sl": sl,
        "extra_4h": extra_4h,
    }


def simulate_highest_tf_only(
    *,
    side: str,
    highest_row: pd.Series,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_times: np.ndarray,
    extra_4h: bool = False,
) -> dict[str, Any]:
    """RETROSPECTIVE_DIAGNOSTIC: enter only at highest TF T0 with its plan."""
    if not bool(highest_row.get("entry_valid", False)):
        return {"exit_type": "INVALID", "net": np.nan, "gross": np.nan, "hold_min": np.nan}
    tf = str(highest_row["signal_tf"])
    ei = int(highest_row["entry_i"])
    epx = float(highest_row["entry_price"])
    tp, sl = tpsl_for_tf(tf, extra_4h=extra_4h and tf == "4h")
    n = len(close)
    end_i = _hold_end_i(ei, open_times, MAX_HOLD_BY_TF[tf], n)
    for bar_i in range(ei + 1, end_i + 1):
        et, eg = _bar_exit(side, epx, float(high[bar_i]), float(low[bar_i]), float(close[bar_i]), tp, sl)
        if et is not None:
            hold = float((open_times[bar_i] - open_times[ei]) / np.timedelta64(1, "m"))
            return {
                "exit_type": et,
                "gross": float(eg),
                "net": float(eg) - FEE_PCT,
                "hold_min": hold,
                "plan_tf_final": tf,
                "n_upgrades": 0,
                "upgrade_sequence": f"ORACLE_{tf}",
                "mark": "RETROSPECTIVE_DIAGNOSTIC",
            }
    hold = float((open_times[end_i] - open_times[ei]) / np.timedelta64(1, "m"))
    g = _dir_ret_open(side, epx, float(close[end_i]))
    return {
        "exit_type": "TIMEOUT",
        "gross": g,
        "net": g - FEE_PCT,
        "hold_min": hold,
        "plan_tf_final": tf,
        "n_upgrades": 0,
        "upgrade_sequence": f"ORACLE_{tf}",
        "mark": "RETROSPECTIVE_DIAGNOSTIC",
    }


def summarize_trades(trades: list[dict[str, Any]], **meta) -> dict[str, Any]:
    nets = np.asarray([t["net"] for t in trades if t.get("exit_type") != "INVALID" and t.get("net") == t.get("net")], dtype=float)
    exits = [t["exit_type"] for t in trades if t.get("exit_type") != "INVALID" and t.get("net") == t.get("net")]
    holds = np.asarray(
        [t["hold_min"] for t in trades if t.get("exit_type") != "INVALID" and t.get("net") == t.get("net")],
        dtype=float,
    )
    n = int(len(nets))
    row: dict[str, Any] = {**meta, "n": n, "sample_flag": sample_flag(n)}
    if n == 0:
        return row
    wins = nets[nets > 0]
    losses = nets[nets < 0]
    eq = np.cumsum(nets)
    dd = eq - np.maximum.accumulate(eq)
    max_dd = float(dd.min()) if len(dd) else 0.0
    max_l = cur = 0
    for x in nets:
        if x < 0:
            cur += 1
            max_l = max(max_l, cur)
        else:
            cur = 0
    row.update(
        {
            "tp_count": int(sum(1 for e in exits if e == "TP")),
            "sl_count": int(sum(1 for e in exits if e == "SL")),
            "timeout_count": int(sum(1 for e in exits if e == "TIMEOUT")),
            "conflict_exit_count": int(
                sum(1 for e in exits if e in ("CONFLICT_EXIT", "HIGHER_TF_DOMINANCE_EXIT"))
            ),
            "mean_net": float(np.mean(nets)),
            "median_net": float(np.median(nets)),
            "expectancy": float(np.mean(nets)),
            "win_rate": float(np.mean(nets > 0)),
            "avg_winner": float(np.mean(wins)) if len(wins) else None,
            "avg_loser": float(np.mean(losses)) if len(losses) else None,
            "profit_factor": (
                float(np.sum(wins) / abs(np.sum(losses)))
                if len(wins) and len(losses) and np.sum(losses) != 0
                else None
            ),
            "max_drawdown": max_dd,
            "max_consecutive_losses": int(max_l),
            "median_hold_min": float(np.median(holds)),
            "q05_net": float(np.quantile(nets, 0.05)),
            "cumulative_net": float(np.sum(nets)),
            "recovery_factor": (
                float(np.sum(nets) / abs(max_dd)) if max_dd < 0 else None
            ),
        }
    )
    return row


def summarize_givebacks(upgrade_events: list[dict[str, Any]], **meta) -> dict[str, Any]:
    if not upgrade_events:
        return {**meta, "n": 0, "sample_flag": sample_flag(0)}
    pau = np.asarray([e["profit_at_upgrade"] for e in upgrade_events], dtype=float)
    gb = np.asarray([e.get("giveback_from_upgrade_profit", 0.0) for e in upgrade_events], dtype=float)
    rg = np.asarray([e.get("realized_gross", np.nan) for e in upgrade_events], dtype=float)
    profitable = pau > 0
    row = {
        **meta,
        "n": int(len(upgrade_events)),
        "sample_flag": sample_flag(len(upgrade_events)),
        "mean_profit_at_upgrade": float(np.mean(pau)),
        "median_profit_at_upgrade": float(np.median(pau)),
        "frac_profit_at_upgrade": float(np.mean(pau > 0)),
        "mean_giveback": float(np.mean(gb)),
        "median_giveback": float(np.median(gb)),
        "mean_giveback_when_open_profit": float(np.mean(gb[profitable])) if np.any(profitable) else None,
        "median_giveback_when_open_profit": float(np.median(gb[profitable])) if np.any(profitable) else None,
        "mean_realized_gross": float(np.nanmean(rg)),
    }
    return row
