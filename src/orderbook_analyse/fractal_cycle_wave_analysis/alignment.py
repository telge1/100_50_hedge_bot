"""Cross-timeframe alignment and re-alignment sequence metrics."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis import INEFFICIENT_ABS_PRICE_PCT, WAVE_TFS


def annotate_alignment(
    waves: pd.DataFrame,
    *,
    child_tf: str,
    parent_tf: str,
    parent_ind: pd.DataFrame,
) -> pd.DataFrame:
    if waves is None or waves.empty:
        return waves.copy() if waves is not None else pd.DataFrame()
    out = waves.copy()
    if parent_ind is None or parent_ind.empty:
        out["parent_tf"] = parent_tf
        out["parent_dir"] = None
        out["alignment"] = "UNKNOWN"
        out["child_tf"] = child_tf
        return out

    parent = parent_ind.sort_values("available_at").reset_index(drop=True)
    p_avail = parent["available_at"].to_numpy(dtype="datetime64[ns]")
    p_dir = parent["stoch_dir"].to_numpy()
    ends = pd.to_datetime(out["end_available_at"], utc=True).to_numpy(dtype="datetime64[ns]")
    # searchsorted right-1 = last parent available_at <= end
    idx = np.searchsorted(p_avail, ends, side="right") - 1
    parents: list[str | None] = []
    aligns: list[str] = []
    for i, wdir in enumerate(out["direction"].to_numpy()):
        j = int(idx[i])
        if j < 0:
            parents.append(None)
            aligns.append("UNKNOWN")
            continue
        pd_ = p_dir[j]
        if pd_ not in ("UP", "DOWN"):
            parents.append(None)
            aligns.append("UNKNOWN")
            continue
        parents.append(str(pd_))
        aligns.append("ALIGNED" if pd_ == wdir else "COUNTER")
    out["parent_tf"] = parent_tf
    out["parent_dir"] = parents
    out["alignment"] = aligns
    out["child_tf"] = child_tf
    return out


def summarize_alignment(aligned: pd.DataFrame) -> dict[str, Any]:
    if aligned is None or aligned.empty:
        return {"n": 0}
    out: dict[str, Any] = {"n": int(len(aligned))}
    for label in ("ALIGNED", "COUNTER", "UNKNOWN"):
        sub = aligned[aligned["alignment"] == label]
        out[label] = {
            "n": int(len(sub)),
            "mean_abs_price_move_pct": float(sub["price_move_pct"].abs().mean()) if len(sub) else None,
            "mean_signed_price_move_pct": float(sub["signed_price_move_pct"].mean()) if len(sub) else None,
            "inefficient_share": float(sub["inefficient_flag"].mean()) if len(sub) else None,
        }
    a = out["ALIGNED"]["mean_abs_price_move_pct"]
    c = out["COUNTER"]["mean_abs_price_move_pct"]
    out["counter_weaker_than_aligned"] = (
        bool(c is not None and a is not None and c < a) if a and c else None
    )
    return out


def re_alignment_sequences(
    aligned: pd.DataFrame,
    *,
    inefficient_abs_price_pct: float = INEFFICIENT_ABS_PRICE_PCT,
) -> dict[str, Any]:
    if aligned is None or aligned.empty:
        return {"n_candidates": 0, "n_realign": 0}
    df = aligned.sort_values("start_ts").reset_index(drop=True)
    candidates = 0
    realign = 0
    follow_signed: list[float] = []
    for i in range(len(df) - 1):
        w = df.iloc[i]
        nxt = df.iloc[i + 1]
        if w["alignment"] != "COUNTER":
            continue
        if not bool(w["inefficient_flag"]) and abs(float(w["price_move_pct"])) > inefficient_abs_price_pct:
            continue
        candidates += 1
        if nxt["alignment"] == "ALIGNED" and nxt["direction"] == w["parent_dir"]:
            realign += 1
            follow_signed.append(float(nxt["signed_price_move_pct"]))
    return {
        "n_candidates": candidates,
        "n_realign": realign,
        "realign_rate": (realign / candidates) if candidates else None,
        "mean_followthrough_signed_pct": float(np.mean(follow_signed)) if follow_signed else None,
        "median_followthrough_signed_pct": float(np.median(follow_signed)) if follow_signed else None,
    }


def default_child_parent_pairs() -> list[tuple[str, str]]:
    pairs = [
        ("1m", "5m"),
        ("5m", "15m"),
        ("15m", "30m"),
        ("30m", "1h"),
        ("1h", "4h"),
        ("4h", "1d"),
        ("1d", "1w"),
        ("1w", "1M"),
    ]
    extra = [(tf, "1d") for tf in WAVE_TFS if tf != "1d"]
    seen = set(pairs)
    for p in extra:
        if p not in seen:
            pairs.append(p)
            seen.add(p)
    return pairs
