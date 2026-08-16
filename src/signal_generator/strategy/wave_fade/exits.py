"""Intrabar TP/SL scan — freeze ``_scan_exit`` (SL_FIRST).

When TP and SL hit on the same 1m bar, SL wins (ambiguous_sl_first=True).
"""

from __future__ import annotations

import numpy as np


def dir_ret(side: str, epx: float, px: float) -> float:
    """Freeze ``_dir_ret``."""
    if side == "LONG":
        return (px / epx - 1.0) * 100.0
    return (epx - px) / epx * 100.0


def hold_end_i(ei: int, open_times: np.ndarray, max_hold_min: int, n: int) -> int:
    """Freeze ``_hold_end_i``."""
    t_end = open_times[ei] + np.timedelta64(int(max_hold_min), "m")
    return min(n - 1, max(ei + 1, int(np.searchsorted(open_times, t_end, side="right") - 1)))


def scan_exit_sl_first(
    side: str,
    epx: float,
    high: np.ndarray,
    low: np.ndarray,
    start_i: int,
    end_i: int,
    tp: float,
    sl: float,
) -> tuple[str | None, float | None, int | None, bool]:
    """Freeze ``_scan_exit``: returns exit_type, gross, exit_i, ambiguous_sl_first."""
    if end_i < start_i:
        return None, None, None, False
    hh = high[start_i : end_i + 1]
    ll = low[start_i : end_i + 1]
    if hh.size == 0:
        return None, None, None, False
    if side == "LONG":
        fav = (hh / epx - 1.0) * 100.0
        adv = (ll / epx - 1.0) * 100.0
    else:
        fav = (epx - ll) / epx * 100.0
        adv = -((hh - epx) / epx * 100.0)
    hit_tp = fav >= tp
    hit_sl = adv <= -sl
    any_tp = bool(np.any(hit_tp))
    any_sl = bool(np.any(hit_sl))
    i_tp = int(np.argmax(hit_tp)) if any_tp else -1
    i_sl = int(np.argmax(hit_sl)) if any_sl else -1
    if not any_tp and not any_sl:
        return None, None, None, False
    if not any_tp or (any_sl and i_sl <= i_tp):
        amb = bool(any_tp and any_sl and i_sl == i_tp)
        return "SL", float(-sl), start_i + i_sl, amb
    return "TP", float(tp), start_i + i_tp, False


# Alias matching freeze private name for callers that prefer it
_scan_exit = scan_exit_sl_first
_dir_ret = dir_ret
_hold_end_i = hold_end_i
