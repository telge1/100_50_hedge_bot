"""Extract drawdown episodes from a chronological equity series."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def max_streak(reasons: list[str], count_as: set[str]) -> int:
    best = cur = 0
    for r in reasons:
        if r in count_as:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return int(best)


def extract_episodes(
    equity: np.ndarray,
    *,
    times: list[pd.Timestamp] | None,
    reasons: list[str] | None,
    trade_ids: list[int] | None,
    start_equity: float,
) -> pd.DataFrame:
    """
    Equity array is post-trade totals (len N). Peak starts at start_equity.
    Episode indices refer to trade positions 0..N-1.
    """
    n = len(equity)
    if n == 0:
        return pd.DataFrame()

    # path includes synthetic start at index -1 / equity[0] prepended
    path = np.concatenate([[float(start_equity)], equity.astype(float)])
    peak = np.maximum.accumulate(path)
    dd = np.where(peak > 0, (path / peak - 1.0) * 100.0, 0.0)
    # underwater on post-trade points only (skip synthetic start)
    underwater = dd[1:] < -1e-12

    rows: list[dict[str, Any]] = []
    i = 0
    ep_id = 0
    while i < n:
        if not underwater[i]:
            i += 1
            continue
        # episode start at trade i (first underwater)
        # peak is the running peak at path index i (before this trade's effect relative to prior peak)
        # peak level for this episode: peak just before going underwater = path peak at index i
        # At trade i, path[i+1] is underwater vs peak[i+1]. The peak value is peak[i+1] which equals
        # the last ATH. Find peak index among 0..i (path indices), preferring last time path hit peak.
        start_i = i
        j = i
        while j < n and underwater[j]:
            j += 1
        end_i = j - 1  # last underwater trade index
        # trough within [start_i, end_i]
        seg_dd = dd[start_i + 1 : end_i + 2]
        trough_rel = int(np.argmin(seg_dd))
        trough_i = start_i + trough_rel
        max_dd = float(seg_dd[trough_rel])
        trough_eq = float(path[trough_i + 1])
        peak_eq = float(peak[trough_i + 1])
        # peak time: last index before/at trough where path == peak_eq
        peak_path_idx = 0
        for k in range(trough_i + 1, -1, -1):
            if abs(path[k] - peak_eq) <= 1e-9 * max(1.0, peak_eq):
                peak_path_idx = k
                break
        # peak_path_idx 0 = synthetic start; trade index = peak_path_idx - 1
        peak_trade_i = peak_path_idx - 1

        recovered = j < n and path[j + 1] >= peak_eq - 1e-9 * max(1.0, peak_eq)
        # if recovered, recovery trade index is j (first trade that restored peak)
        # Actually: underwater ends when dd[j] is false for j==end of underwater.
        # When j < n, trade j is first non-underwater, i.e. equity recovered to peak.
        recovery_i = j if recovered else None

        def _t(idx: int | None):
            if idx is None or times is None:
                return None
            if idx < 0:
                return None  # before first trade
            return times[idx]

        def _tid(idx: int | None):
            if idx is None or trade_ids is None:
                return None
            if idx < 0:
                return None
            return int(trade_ids[idx])

        # trades in episode window: from first underwater through trough / recovery
        # For reason counts use start_i .. end_i (underwater trades), and for recovery include to recovery_i
        win_end = recovery_i if recovery_i is not None else end_i
        win_reasons = reasons[start_i : win_end + 1] if reasons is not None else []
        # streaks within underwater window (peak->recovery including underwater phase)
        streak_reasons = reasons[start_i : end_i + 1] if reasons is not None else []

        peak_time = _t(peak_trade_i) if peak_trade_i >= 0 else None
        # if peak at synthetic start, use start_i time as proxy labeled separately
        if peak_trade_i < 0 and times is not None:
            peak_time = times[start_i]

        start_time = _t(start_i)
        trough_time = _t(trough_i)
        recovery_time = _t(recovery_i)

        def _dur(a, b):
            if a is None or b is None:
                return None
            return float((pd.Timestamp(b) - pd.Timestamp(a)).total_seconds() / 3600.0)

        ep_id += 1
        rows.append(
            {
                "episode_id": ep_id,
                "peak_trade_index": int(peak_trade_i),
                "start_trade_index": int(start_i),
                "trough_trade_index": int(trough_i),
                "recovery_trade_index": None if recovery_i is None else int(recovery_i),
                "peak_trade_id": _tid(peak_trade_i),
                "start_trade_id": _tid(start_i),
                "trough_trade_id": _tid(trough_i),
                "recovery_trade_id": _tid(recovery_i),
                "peak_time": peak_time,
                "start_time": start_time,
                "trough_time": trough_time,
                "recovery_time": recovery_time,
                "peak_equity": peak_eq,
                "trough_equity": trough_eq,
                "loss_abs": float(peak_eq - trough_eq),
                "max_drawdown_pct": max_dd,
                "fully_recovered": bool(recovered),
                "duration_to_trough_hours": _dur(peak_time, trough_time),
                "duration_trough_to_recovery_hours": _dur(trough_time, recovery_time),
                "duration_to_recovery_hours": _dur(peak_time, recovery_time),
                "trades_to_trough": int(trough_i - max(peak_trade_i, 0))
                if peak_trade_i >= 0
                else int(trough_i - start_i + 1),
                "trades_to_recovery": None
                if recovery_i is None
                else int(recovery_i - max(peak_trade_i, 0)),
                "trades_in_underwater": int(end_i - start_i + 1),
                "n_tp": int(sum(1 for r in streak_reasons if r == "TP")),
                "n_sl": int(sum(1 for r in streak_reasons if r == "SL")),
                "n_be": int(sum(1 for r in streak_reasons if r == "BE")),
                "longest_true_sl_streak": max_streak(streak_reasons, {"SL"}),
                "longest_non_winner_streak": max_streak(streak_reasons, {"SL", "BE"}),
            }
        )
        i = j if j > i else i + 1

    return pd.DataFrame(rows)
