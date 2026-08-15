"""Control groups C1–C5 (diagnostic matching on causal features only)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def sample_controls(
    df: pd.DataFrame,
    *,
    events: list[dict[str, Any]],
    rng: np.random.Generator | None = None,
    max_per_event: int = 1,
) -> list[dict[str, Any]]:
    """Build simple control samples for smoke/full audit.

    Matching uses coin + ATR quartile + hour-of-day only (no outcomes).
    """
    rng = rng or np.random.default_rng(42)
    if df.empty or not events:
        return []

    d = df.reset_index(drop=True)
    atr = d["atr_14"].to_numpy(dtype=float)
    finite = np.isfinite(atr)
    if finite.sum() < 4:
        atr_q = np.zeros(len(d), dtype=int)
    else:
        try:
            atr_q = pd.qcut(pd.Series(atr).where(finite), 4, labels=False, duplicates="drop").fillna(0).astype(int).to_numpy()
        except ValueError:
            atr_q = np.zeros(len(d), dtype=int)
    hours = pd.to_datetime(d["bucket_start"], utc=True).dt.hour.to_numpy()

    # precompute burst mask any B1
    any_burst = (
        d.get("B1_long", False).to_numpy(dtype=bool)
        | d.get("B1_short", False).to_numpy(dtype=bool)
    )
    vol_burst = d["volume_burst"].to_numpy(dtype=float) >= np.nanpercentile(
        d["volume_burst"].to_numpy(dtype=float)[np.isfinite(d["volume_burst"].to_numpy(dtype=float))],
        95,
    ) if np.isfinite(d["volume_burst"]).sum() > 10 else np.zeros(len(d), dtype=bool)

    out: list[dict[str, Any]] = []
    for ev in events:
        sym = ev["symbol"]
        side = ev["side"]
        ai = int(ev["anchor_index"])
        mask_sym = (d["symbol"] == sym).to_numpy()
        q = atr_q[ai] if 0 <= ai < len(atr_q) else 0
        h = int(hours[ai]) if 0 <= ai < len(hours) else 0
        base = mask_sym & (atr_q == q) & (hours == h)

        # C4 random same coin
        cand4 = np.where(mask_sym & (np.arange(len(d)) != ai))[0]
        if len(cand4):
            j = int(rng.choice(cand4))
            out.append({**_ctrl_row(d, j, "C4", ev), "matched_event_anchor": ev.get("anchor_bucket")})

        # C1 price move without burst
        ret = d["ret_5m_pct"].to_numpy(dtype=float)
        if side == "long":
            px = ret < 0
        else:
            px = ret > 0
        cand1 = np.where(base & px & ~any_burst)[0]
        if len(cand1):
            j = int(rng.choice(cand1))
            out.append({**_ctrl_row(d, j, "C1", ev), "matched_event_anchor": ev.get("anchor_bucket")})

        # C2 burst without OI drop
        oi = d["oi_chg_5m"].to_numpy(dtype=float)
        cand2 = np.where(base & any_burst & np.isfinite(oi) & (oi >= 0))[0]
        if len(cand2):
            j = int(rng.choice(cand2))
            out.append({**_ctrl_row(d, j, "C2", ev), "matched_event_anchor": ev.get("anchor_bucket")})

        # C5 volume burst without liq burst
        cand5 = np.where(base & vol_burst & ~any_burst)[0]
        if len(cand5):
            j = int(rng.choice(cand5))
            out.append({**_ctrl_row(d, j, "C5", ev), "matched_event_anchor": ev.get("anchor_bucket")})

    return out


def _ctrl_row(d: pd.DataFrame, j: int, kind: str, ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "control": kind,
        "symbol": str(d["symbol"].iloc[j]),
        "bucket_start": str(d["bucket_start"].iloc[j]),
        "index": int(j),
        "side": ev.get("side"),
        "burst": ev.get("burst"),
    }
