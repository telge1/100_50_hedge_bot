"""Multi-horizon causal MFE/MAE research metrics (positive pct, MAE_FIRST on ties)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

HORIZONS_MIN = (15, 30, 60, 120, 240)
TARGET_THRESHOLDS = (0.10, 0.20, 0.30, 0.40, 0.50)
ADVERSE_THRESHOLDS = (0.10, 0.20, 0.30, 0.40, 0.50)
FIRST_HIT_PAIRS = (
    (0.20, 0.20),
    (0.25, 0.20),
    (0.30, 0.20),
    (0.30, 0.25),
    (0.40, 0.30),
)


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _path(candles_1m: pd.DataFrame, entry_at: datetime, horizon_min: int) -> pd.DataFrame:
    entry_ts = _utc(entry_at)
    end = entry_ts + timedelta(minutes=int(horizon_min))
    if candles_1m is None or candles_1m.empty:
        return pd.DataFrame()
    tcol = pd.to_datetime(candles_1m["open_time"])
    if getattr(tcol.dt, "tz", None) is not None:
        tcol = tcol.dt.tz_convert("UTC")
        a, b = entry_ts, end
    else:
        a, b = entry_ts.replace(tzinfo=None), end.replace(tzinfo=None)
    mask = (tcol >= pd.Timestamp(a)) & (tcol < pd.Timestamp(b))
    return candles_1m.loc[mask].sort_values("open_time")


def compute_mfe_mae_horizon(
    candles_1m: pd.DataFrame,
    *,
    direction: str,
    entry_at: datetime | str,
    entry_price: float,
    horizon_min: int,
) -> dict[str, Any]:
    bull = str(direction).upper() == "BULLISH"
    px = float(entry_price)
    entry_ts = _utc(entry_at)
    path = _path(candles_1m, entry_ts, horizon_min)
    base = {
        "horizon_min": int(horizon_min),
        "entry_at": entry_ts.isoformat(),
        "entry_price": px,
        "coverage": "EMPTY" if path.empty else "OK",
    }
    if path.empty or px <= 0:
        return {
            **base,
            "mfe_pct": None,
            "mae_pct": None,
            "mfe_at": None,
            "mae_at": None,
            "close_return_pct": None,
            "mfe_minus_mae": None,
            "mfe_mae_ratio": None,
            "first_extreme": None,
            "minutes_to_mfe": None,
            "minutes_to_mae": None,
            "targets_hit": {},
            "adverse_hit": {},
            "first_hit_pairs": {},
        }

    mfe = mae = 0.0
    mfe_at = mae_at = None
    mfe_min = mae_min = None
    # walk bar by bar for first-extreme and first-hit
    first_extreme = None
    hit_target: dict[str, bool] = {f"{t:.2f}": False for t in TARGET_THRESHOLDS}
    hit_adverse: dict[str, bool] = {f"{t:.2f}": False for t in ADVERSE_THRESHOLDS}
    pair_result: dict[str, str] = {f"t{a:.2f}_a{b:.2f}": "NEITHER" for a, b in FIRST_HIT_PAIRS}
    pair_done = {k: False for k in pair_result}

    for _, row in path.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        ts = pd.Timestamp(row["open_time"]).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        mins = (ts - entry_ts).total_seconds() / 60.0

        if bull:
            fav = (high / px - 1.0) * 100.0
            adv = (px / low - 1.0) * 100.0 if low > 0 else 0.0
            # same-bar: adverse and favor — MAE_FIRST / ADVERSE_FIRST
            bar_hit_adv = adv
            bar_hit_fav = fav
        else:
            fav = (px / low - 1.0) * 100.0 if low > 0 else 0.0
            adv = (high / px - 1.0) * 100.0
            bar_hit_adv = adv
            bar_hit_fav = fav

        if fav > mfe:
            mfe = fav
            mfe_at = ts
            mfe_min = mins
        if adv > mae:
            mae = adv
            mae_at = ts
            mae_min = mins

        if first_extreme is None:
            # any positive movement extreme update on this bar
            fav_moved = bar_hit_fav > 0
            adv_moved = bar_hit_adv > 0
            if fav_moved and adv_moved:
                first_extreme = "MAE_FIRST"
            elif adv_moved:
                first_extreme = "MAE_FIRST"
            elif fav_moved:
                first_extreme = "MFE_FIRST"

        for t in TARGET_THRESHOLDS:
            key = f"{t:.2f}"
            if not hit_target[key] and bar_hit_fav >= t:
                hit_target[key] = True
        for t in ADVERSE_THRESHOLDS:
            key = f"{t:.2f}"
            if not hit_adverse[key] and bar_hit_adv >= t:
                hit_adverse[key] = True

        for tp, sl in FIRST_HIT_PAIRS:
            pk = f"t{tp:.2f}_a{sl:.2f}"
            if pair_done[pk]:
                continue
            hit_t = bar_hit_fav >= tp
            hit_a = bar_hit_adv >= sl
            if hit_t and hit_a:
                pair_result[pk] = "ADVERSE_FIRST"
                pair_done[pk] = True
            elif hit_a:
                pair_result[pk] = "ADVERSE_FIRST"
                pair_done[pk] = True
            elif hit_t:
                pair_result[pk] = "TARGET_FIRST"
                pair_done[pk] = True

    last_close = float(path.iloc[-1]["close"])
    if bull:
        close_ret = (last_close / px - 1.0) * 100.0
    else:
        close_ret = (px / last_close - 1.0) * 100.0 if last_close > 0 else 0.0

    ratio = (mfe / mae) if mae > 1e-12 else (float("inf") if mfe > 0 else None)
    return {
        **base,
        "mfe_pct": round(mfe, 6),
        "mae_pct": round(mae, 6),
        "mfe_at": mfe_at.isoformat() if mfe_at else None,
        "mae_at": mae_at.isoformat() if mae_at else None,
        "close_return_pct": round(close_ret, 6),
        "mfe_minus_mae": round(mfe - mae, 6),
        "mfe_mae_ratio": (round(ratio, 6) if ratio is not None and ratio != float("inf") else ratio),
        "first_extreme": first_extreme or "NEITHER",
        "minutes_to_mfe": mfe_min,
        "minutes_to_mae": mae_min,
        "targets_hit": hit_target,
        "adverse_hit": hit_adverse,
        "first_hit_pairs": pair_result,
    }


def compute_all_horizons(
    candles_1m: pd.DataFrame,
    *,
    direction: str,
    entry_at: datetime | str,
    entry_price: float,
) -> dict[str, dict[str, Any]]:
    return {
        str(h): compute_mfe_mae_horizon(
            candles_1m,
            direction=direction,
            entry_at=entry_at,
            entry_price=entry_price,
            horizon_min=h,
        )
        for h in HORIZONS_MIN
    }
