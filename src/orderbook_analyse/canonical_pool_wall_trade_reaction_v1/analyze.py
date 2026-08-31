"""Per-episode wall-in-pool + touch + reaction labeling."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.contracts import (
    EATEN_TRADE_FRAC,
    FORWARD_SECONDS,
    PASS_BACK_EDGE,
    PULLED_TRADE_FRAC,
    REJECT_REVERSAL_ZONE_FRAC,
    TOUCH_TOLERANCE_BPS,
    WALL_IN_ZONE_MIN_FRAC,
    WALL_LOOKAROUND_SECONDS,
)


def front_back(side: str, lower: float, upper: float) -> tuple[float, float]:
    side = str(side).upper()
    if side == "BID":
        return float(upper), float(lower)  # front, back
    return float(lower), float(upper)


def analyze_episode(
    ep: dict[str, Any],
    feat: pd.DataFrame,
    trades: pd.DataFrame,
) -> dict[str, Any]:
    """Mechanical labels for one pool episode. feat/trades indexed by timestamp."""
    side = str(ep["side"]).upper()
    lower = float(ep["lower"])
    upper = float(ep["upper"])
    front, back = front_back(side, lower, upper)
    zone_h = max(upper - lower, 1e-9)
    t0 = pd.Timestamp(ep["first_seen"])
    t1 = pd.Timestamp(ep["last_seen"])
    if t0.tzinfo is None:
        t0 = t0.tz_localize("UTC")
    if t1.tzinfo is None:
        t1 = t1.tz_localize("UTC")

    out: dict[str, Any] = {
        "pool_id": ep["pool_id"],
        "timeframe": ep["timeframe"],
        "side": side,
        "lower": lower,
        "upper": upper,
        "front_edge": front,
        "back_edge": back,
        "zone_height": zone_h,
        "first_seen": t0.isoformat().replace("+00:00", "Z"),
        "last_seen": t1.isoformat().replace("+00:00", "Z"),
        "maximum_P": int(ep.get("maximum_P") or 0),
        "maximum_age_closed_bars": int(ep.get("maximum_age_closed_bars") or 0),
        "class_tags_str": ep.get("class_tags_str") or "",
        "feature_seconds": 0,
        "wall_in_zone_seconds": 0,
        "wall_in_zone_frac": None,
        "wall_in_pool": "NO_DATA",
        "median_wall_qty_in_zone": None,
        "median_wall_notional_in_zone": None,
        "max_wall_notional_in_zone": None,
        "first_touch_ts": None,
        "touched": False,
        "wall_fate": "NO_TOUCH",
        "wall_notional_at_touch": None,
        "wall_notional_drop": None,
        "trade_notional_into_wall": None,
        "reaction": "NO_TOUCH",
        "max_penetration_pct": None,
        "max_reversal_zone_frac": None,
        "back_edge_crossed": False,
        "seconds_to_touch": None,
        "forward_seconds_used": FORWARD_SECONDS,
    }

    if feat.empty:
        return out

    # Episode observation window: first_seen → last_seen (+ small pad for forward)
    win_end = t1 + pd.Timedelta(seconds=FORWARD_SECONDS)
    try:
        sl = feat.loc[t0:win_end]
    except Exception:
        sl = feat[(feat["bucket_start"] >= t0) & (feat["bucket_start"] <= win_end)]
    if sl.empty:
        return out

    mid = sl["mid_price"].to_numpy(dtype=float)
    times = sl["bucket_start"]
    if side == "BID":
        wpx = sl["bid_wall_price"].to_numpy(dtype=float)
        wqty = sl["bid_wall_qty"].to_numpy(dtype=float)
        wnot = sl["bid_wall_notional"].to_numpy(dtype=float)
    else:
        wpx = sl["ask_wall_price"].to_numpy(dtype=float)
        wqty = sl["ask_wall_qty"].to_numpy(dtype=float)
        wnot = sl["ask_wall_notional"].to_numpy(dtype=float)

    # Restrict wall stats to episode lifetime [t0, t1]
    life_mask = (times >= t0) & (times <= t1)
    life_n = int(life_mask.sum())
    out["feature_seconds"] = life_n
    if life_n == 0:
        return out

    in_zone = (wpx >= lower) & (wpx <= upper) & np.isfinite(wpx)
    in_zone_life = in_zone & life_mask.to_numpy()
    wiz = int(in_zone_life.sum())
    out["wall_in_zone_seconds"] = wiz
    frac = wiz / life_n
    out["wall_in_zone_frac"] = round(float(frac), 4)
    out["wall_in_pool"] = "YES" if frac >= WALL_IN_ZONE_MIN_FRAC else "NO"
    if wiz > 0:
        out["median_wall_qty_in_zone"] = float(np.nanmedian(wqty[in_zone_life]))
        out["median_wall_notional_in_zone"] = float(np.nanmedian(wnot[in_zone_life]))
        out["max_wall_notional_in_zone"] = float(np.nanmax(wnot[in_zone_life]))

    # First touch: mid reaches the front edge within TOUCH_TOLERANCE_BPS
    # (NOT within a full zone-height — that falsely fired far from the pool).
    tol_px = max(front * TOUCH_TOLERANCE_BPS / 1e4, zone_h * 0.02)
    if side == "BID":
        # BID pool typically below: price falls into front from above
        touch = (mid <= front + tol_px) & (mid >= back - tol_px)
    else:
        # ASK pool typically above: price rises into front from below
        touch = (mid >= front - tol_px) & (mid <= back + tol_px)

    touch_life = touch & life_mask.to_numpy()
    if touch_life.any():
        idx = int(np.flatnonzero(touch_life)[0])
    elif touch.any():
        idx = int(np.flatnonzero(touch)[0])
    else:
        return out

    touch_ts = pd.Timestamp(times.iloc[idx])
    out["touched"] = True
    out["first_touch_ts"] = touch_ts.isoformat().replace("+00:00", "Z")
    out["seconds_to_touch"] = int((touch_ts - t0).total_seconds())

    # Wall fate around touch (±60s)
    i0 = max(0, idx - WALL_LOOKAROUND_SECONDS)
    i1 = min(len(wnot) - 1, idx + WALL_LOOKAROUND_SECONDS)
    wall_before = wnot[i0 : idx + 1]
    wall_after = wnot[idx : i1 + 1]
    w0 = float(np.nanmax(wall_before)) if len(wall_before) else float("nan")
    w1 = float(np.nanmin(wall_after)) if len(wall_after) else float("nan")
    drop = max(0.0, w0 - w1) if np.isfinite(w0) and np.isfinite(w1) else 0.0
    out["wall_notional_at_touch"] = w0 if np.isfinite(w0) else None
    out["wall_notional_drop"] = round(drop, 2)

    # Aggressive notional into the wall (Buy into ASK, Sell into BID)
    t_a = touch_ts - pd.Timedelta(seconds=WALL_LOOKAROUND_SECONDS)
    t_b = touch_ts + pd.Timedelta(seconds=WALL_LOOKAROUND_SECONDS)
    trade_into = 0.0
    if not trades.empty:
        try:
            tr = trades.loc[t_a:t_b]
        except Exception:
            tr = trades[(trades["second"] >= t_a) & (trades["second"] <= t_b)]
        if not tr.empty:
            if side == "ASK":
                trade_into = float(tr["buy_notional"].sum())
            else:
                trade_into = float(tr["sell_notional"].sum())
    out["trade_notional_into_wall"] = round(trade_into, 2)

    if drop <= 0 and (not np.isfinite(w0) or w0 <= 0):
        out["wall_fate"] = "NO_WALL"
    elif drop <= 0:
        out["wall_fate"] = "HELD"
    elif w0 > 0 and trade_into >= EATEN_TRADE_FRAC * drop:
        out["wall_fate"] = "EATEN"
    elif w0 > 0 and trade_into <= PULLED_TRADE_FRAC * drop:
        out["wall_fate"] = "PULLED"
    else:
        out["wall_fate"] = "MIXED"

    # Forward reaction after touch
    fwd_end = touch_ts + pd.Timedelta(seconds=FORWARD_SECONDS)
    fwd_mask = (times >= touch_ts) & (times <= fwd_end)
    if not fwd_mask.any():
        out["reaction"] = "AMBIGUOUS"
        return out
    fwd_mid = mid[fwd_mask.to_numpy()]
    if side == "BID":
        # penetration down into zone from front toward back
        penetration = np.clip((front - fwd_mid) / zone_h, 0, None)
        reversal = np.clip((fwd_mid - front) / zone_h, 0, None)  # bounce up above front
        crossed = bool(np.any(fwd_mid <= back)) if PASS_BACK_EDGE else False
    else:
        penetration = np.clip((fwd_mid - front) / zone_h, 0, None)
        reversal = np.clip((front - fwd_mid) / zone_h, 0, None)
        crossed = bool(np.any(fwd_mid >= back)) if PASS_BACK_EDGE else False

    out["max_penetration_pct"] = round(float(np.nanmax(penetration)) * 100.0, 2)
    out["max_reversal_zone_frac"] = round(float(np.nanmax(reversal)), 4)
    out["back_edge_crossed"] = crossed

    if crossed:
        out["reaction"] = "PASSED_THROUGH"
    elif float(np.nanmax(reversal)) >= REJECT_REVERSAL_ZONE_FRAC:
        out["reaction"] = "REJECTED"
    else:
        out["reaction"] = "AMBIGUOUS"

    return out


def analyze_all(episodes: pd.DataFrame, feat: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    n = len(episodes)
    for i, (_, ep) in enumerate(episodes.iterrows(), start=1):
        if i % 200 == 0 or i == 1 or i == n:
            print(f"  analyze {i}/{n}", flush=True)
        rows.append(analyze_episode(ep.to_dict(), feat, trades))
    return pd.DataFrame(rows)
