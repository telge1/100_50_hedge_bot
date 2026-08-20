"""Pure price/path metrics for market-event reports (no I/O)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

PRE_WINDOWS_M: tuple[int, ...] = (15, 5, 1)
FUTURE_HORIZONS_M: tuple[int, ...] = (5, 15, 30, 60, 240)
PATH_HORIZONS_M: tuple[int, ...] = (60, 240)


def _finite(x: Any) -> bool:
    try:
        return bool(np.isfinite(float(x)))
    except (TypeError, ValueError):
        return False


def safe_pct(numer: float, denom: float) -> float | None:
    if not _finite(numer) or not _finite(denom) or float(denom) == 0.0:
        return None
    return float(numer) / float(denom) - 1.0


def window_return(close_start: float, close_end: float) -> float | None:
    return safe_pct(close_end, close_start)


def window_range_pct(high: float, low: float, ref: float) -> float | None:
    if not _finite(high) or not _finite(low) or not _finite(ref) or float(ref) == 0.0:
        return None
    return (float(high) - float(low)) / float(ref)


def path_window_bars(
    candles: pd.DataFrame,
    *,
    event_open_time: pd.Timestamp | Any,
    horizon_m: int,
    include_event_minute: bool = False,
) -> pd.DataFrame:
    """Return 1m bars used for a post-event path of length ``horizon_m``.

    Default (research convention aligned with frozen hard-tests):
    path starts at the **next** minute after the event open_time
    (entry ≈ next open after event close), length ``horizon_m``.

    If ``include_event_minute`` is True, the event bar itself is included
    and the window is still capped at ``horizon_m`` bars.
    """
    if candles.empty or horizon_m <= 0:
        return candles.iloc[0:0].copy()
    df = candles.sort_values("open_time").reset_index(drop=True)
    t = pd.Timestamp(event_open_time)
    if include_event_minute:
        mask = df["open_time"] >= t
    else:
        mask = df["open_time"] > t
    out = df.loc[mask].head(int(horizon_m)).copy()
    return out


def mfe_mae_for_side(
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    side: str,
) -> dict[str, Any]:
    """MFE/MAE/ret and first-touch times (1-indexed minutes into path).

    LONG: MFE = max(high/entry - 1), MAE = max(1 - low/entry)
    SHORT: MFE = max(1 - low/entry), MAE = max(high/entry - 1)
    """
    side_u = str(side).upper()
    if side_u not in {"LONG", "SHORT"}:
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")
    if not _finite(entry) or float(entry) <= 0 or len(highs) == 0:
        return {
            "side": side_u,
            "entry": float(entry) if _finite(entry) else None,
            "n_bars": int(len(highs)),
            "mfe": None,
            "mae": None,
            "ret": None,
            "time_to_mfe_m": None,
            "time_to_mae_m": None,
            "future_high": None,
            "future_low": None,
            "future_close": None,
        }

    highs = np.asarray(highs, dtype="float64")
    lows = np.asarray(lows, dtype="float64")
    closes = np.asarray(closes, dtype="float64")
    entry_f = float(entry)

    if side_u == "SHORT":
        mfe_path = 1.0 - lows / entry_f
        mae_path = highs / entry_f - 1.0
        ret = (entry_f - float(closes[-1])) / entry_f if _finite(closes[-1]) else None
    else:
        mfe_path = highs / entry_f - 1.0
        mae_path = 1.0 - lows / entry_f
        ret = (float(closes[-1]) - entry_f) / entry_f if _finite(closes[-1]) else None

    mfe = float(np.nanmax(mfe_path)) if np.any(np.isfinite(mfe_path)) else None
    mae = float(np.nanmax(mae_path)) if np.any(np.isfinite(mae_path)) else None

    t_mfe = t_mae = None
    if mfe is not None:
        for k in range(len(mfe_path)):
            if np.isfinite(mfe_path[k]) and mfe_path[k] >= mfe - 1e-15:
                t_mfe = k + 1
                break
    if mae is not None:
        for k in range(len(mae_path)):
            if np.isfinite(mae_path[k]) and mae_path[k] >= mae - 1e-15:
                t_mae = k + 1
                break

    return {
        "side": side_u,
        "entry": entry_f,
        "n_bars": int(len(highs)),
        "mfe": mfe,
        "mae": mae,
        "ret": ret,
        "time_to_mfe_m": t_mfe,
        "time_to_mae_m": t_mae,
        "future_high": float(np.nanmax(highs)) if np.any(np.isfinite(highs)) else None,
        "future_low": float(np.nanmin(lows)) if np.any(np.isfinite(lows)) else None,
        "future_close": float(closes[-1]) if _finite(closes[-1]) else None,
    }


def mfe_mae_both_sides(
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
) -> dict[str, Any]:
    return {
        "LONG": mfe_mae_for_side(entry, highs, lows, closes, "LONG"),
        "SHORT": mfe_mae_for_side(entry, highs, lows, closes, "SHORT"),
    }


def _slice_pre(candles: pd.DataFrame, event_t: pd.Timestamp, minutes: int) -> pd.DataFrame:
    """Closed bars strictly before event open_time, last ``minutes`` bars."""
    pre = candles.loc[candles["open_time"] < event_t].tail(int(minutes))
    return pre


def pre_post_price_metrics(
    candles: pd.DataFrame,
    *,
    event_open_time: Any,
) -> dict[str, Any]:
    """Price metrics split into known-before / event / after (no lookahead mix)."""
    if candles.empty:
        return {"available": False, "reason": "no_candles"}

    df = candles.sort_values("open_time").reset_index(drop=True).copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    event_t = pd.Timestamp(event_open_time)
    event_rows = df.loc[df["open_time"] == event_t]
    if event_rows.empty:
        return {
            "available": False,
            "reason": "event_minute_missing",
            "event_open_time": str(event_t),
        }

    ev = event_rows.iloc[0]
    event_open = float(ev["open"])
    event_close = float(ev["close"])
    event_high = float(ev["high"])
    event_low = float(ev["low"])

    out: dict[str, Any] = {
        "available": True,
        "event_open_time": str(event_t),
        "known_before_event": {},
        "event_minute": {
            "open": event_open,
            "high": event_high,
            "low": event_low,
            "close": event_close,
            "event_minute_return": safe_pct(event_close, event_open),
            "event_minute_range_pct": window_range_pct(event_high, event_low, event_open),
            "note": "Event-minute OHLC is labeled separately; not used as pre-event feature.",
        },
        "after_event": {},
        "path_metrics": {},
        "causality": {
            "pre_features_use_open_time_lt_event": True,
            "path_starts_after_event_minute": True,
            "entry_for_path": "next_1m_open_after_event",
        },
    }

    for w in PRE_WINDOWS_M:
        pre = _slice_pre(df, event_t, w)
        block: dict[str, Any] = {"n_bars": int(len(pre)), "window_m": w}
        if len(pre) >= 1:
            first_close = float(pre.iloc[0]["close"])
            last_close = float(pre.iloc[-1]["close"])
            # Return over the pre window: from close of first bar to close of last bar
            # (all bars have open_time < event).
            block["return"] = window_return(first_close, last_close)
            # Alternative: close just before event vs close w minutes earlier
            if len(pre) >= w:
                ref = float(pre.iloc[0]["open"]) if w == 1 else float(pre.iloc[0]["close"])
                # Prefer close_end / close_start where start is the bar that opened w minutes before end
                end_close = float(pre.iloc[-1]["close"])
                start_close = float(pre.iloc[0]["close"])
                block["return"] = window_return(start_close, end_close)
                block["range_pct"] = window_range_pct(
                    float(pre["high"].max()), float(pre["low"].min()), start_close
                )
                block["ref_close_start"] = start_close
                block["ref_close_end"] = end_close
            else:
                block["range_pct"] = window_range_pct(
                    float(pre["high"].max()), float(pre["low"].min()), last_close
                )
                block["incomplete_window"] = True
        else:
            block["return"] = None
            block["range_pct"] = None
        out["known_before_event"][f"{w}m"] = block

    # Convenience aliases requested by inventory
    for w in PRE_WINDOWS_M:
        b = out["known_before_event"][f"{w}m"]
        out["known_before_event"][f"return_{w}m"] = b.get("return")
        out["known_before_event"][f"range_{w}m"] = b.get("range_pct")

    # Next open after event = entry for path metrics
    post = df.loc[df["open_time"] > event_t].reset_index(drop=True)
    entry = float(post.iloc[0]["open"]) if not post.empty else None
    out["after_event"]["entry_next_open"] = entry
    out["after_event"]["entry_note"] = (
        "Path MFE/MAE/returns use next 1m open after event minute (no event-close lookahead as entry)."
    )

    for h in FUTURE_HORIZONS_M:
        path = post.head(h)
        key = f"future_return_{h}m"
        if entry is None or path.empty or len(path) < h:
            out["after_event"][key] = None
            out["after_event"][f"future_return_{h}m_incomplete"] = True
            if not path.empty and entry is not None and _finite(path.iloc[-1]["close"]):
                out["after_event"][key] = safe_pct(float(path.iloc[-1]["close"]), entry)
        else:
            out["after_event"][key] = safe_pct(float(path.iloc[h - 1]["close"]), entry)

    for h in PATH_HORIZONS_M:
        path = post.head(h)
        if entry is None or path.empty:
            out["path_metrics"][f"{h}m"] = {"available": False}
            continue
        highs = path["high"].to_numpy(dtype="float64")
        lows = path["low"].to_numpy(dtype="float64")
        closes = path["close"].to_numpy(dtype="float64")
        both = mfe_mae_both_sides(entry, highs, lows, closes)
        out["path_metrics"][f"{h}m"] = {
            "available": True,
            "horizon_m": h,
            "n_bars": int(len(path)),
            "entry": entry,
            **both,
        }

    return out


def assert_pre_features_exclude_future(
    feature_times: list[Any],
    event_open_time: Any,
) -> None:
    """Raise if any pre-feature timestamp is at/after the event open_time."""
    event_t = pd.Timestamp(event_open_time)
    for t in feature_times:
        if pd.Timestamp(t) >= event_t:
            raise AssertionError(f"lookahead: feature time {t} >= event {event_t}")
