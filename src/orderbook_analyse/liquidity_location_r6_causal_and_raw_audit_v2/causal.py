"""Causal decision variants + near-edge reclaim / absorption (no leakage)."""

from __future__ import annotations

from typing import Any

import pandas as pd

ABSORPTION_CONTINUATION_MAX = 0.0005


def _naive_utc(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def decision_at_for_variant(
    first_touch_at: Any,
    variant: str,
    *,
    candles_1m: pd.DataFrame | None = None,
) -> tuple[pd.Timestamp, str]:
    """Return (decision_at, status). Variants never mixed."""
    t2 = _naive_utc(first_touch_at)
    if pd.isna(t2):
        return pd.NaT, "missing_first_touch"
    if variant == "SUBMINUTE_30S":
        return t2 + pd.Timedelta(seconds=30), "ok"
    if variant == "CLOSED_1M":
        # End of first fully closed 1m candle after first touch.
        # Candle with open_time T closes at T+1m; require close_time > first_touch.
        if candles_1m is None or candles_1m.empty:
            return pd.NaT, "missing_candles"
        opens = candles_1m["open_time"].map(_naive_utc)
        # first closed candle whose close_time > t2
        for ot in opens.sort_values():
            close_t = ot + pd.Timedelta(minutes=1)
            if close_t > t2:
                return close_t, "ok"
        return pd.NaT, "no_closed_1m_after_touch"
    if variant == "CLOSED_3M":
        if candles_1m is None or candles_1m.empty:
            return pd.NaT, "missing_candles"
        opens = candles_1m["open_time"].map(_naive_utc)
        closed_after = []
        for ot in opens.sort_values():
            close_t = ot + pd.Timedelta(minutes=1)
            if close_t > t2:
                closed_after.append(close_t)
            if len(closed_after) >= 3:
                return closed_after[2], "ok"
        return pd.NaT, "insufficient_closed_1m_bars"
    raise ValueError(f"unknown variant {variant}")


def _trades_until(
    trades: pd.DataFrame, *, end: pd.Timestamp, start: pd.Timestamp | None = None
) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame()
    ts = trades["trade_ts"].map(_naive_utc)
    mask = ts <= end
    if start is not None:
        mask &= ts >= start
    out = trades.loc[mask].copy()
    out["_ts"] = ts.loc[mask]
    return out.sort_values(["_ts", "trade_id"] if "trade_id" in out.columns else ["_ts"])


def causal_price_from_trades(
    trades: pd.DataFrame,
    *,
    decision_at: pd.Timestamp,
    window_start: pd.Timestamp,
    method: str = "last_trade",
) -> dict[str, Any]:
    """Price strictly from trades with trade_ts <= decision_at."""
    sl = _trades_until(trades, end=decision_at, start=window_start)
    if sl.empty:
        return {
            "price": None,
            "price_source": None,
            "price_source_timestamp": None,
            "last_trade_at": None,
            "trade_count": 0,
            "missingness": "NO_TRADES_IN_WINDOW",
            "causal_ok": False,
            "max_source_timestamp": None,
        }
    last = sl.iloc[-1]
    last_at = _naive_utc(last["_ts"])
    if method == "vwap":
        notional = (sl["price"].astype(float) * sl["size"].astype(float)).sum()
        qty = sl["size"].astype(float).sum()
        px = float(notional / qty) if qty > 0 else float(last["price"])
        src = "trade_vwap"
    else:
        px = float(last["price"])
        src = "last_trade"
    max_ts = last_at
    causal_ok = bool(max_ts <= decision_at)
    return {
        "price": px,
        "price_source": src,
        "price_source_timestamp": max_ts.isoformat(),
        "last_trade_at": last_at.isoformat(),
        "trade_count": int(len(sl)),
        "missingness": None,
        "causal_ok": causal_ok,
        "max_source_timestamp": max_ts.isoformat(),
    }


def near_edge_reclaim_subminute(
    trades: pd.DataFrame,
    *,
    side: str,
    near_edge: float,
    first_touch_at: Any,
    decision_at: pd.Timestamp,
    price_method: str = "last_trade",
) -> dict[str, Any]:
    t2 = _naive_utc(first_touch_at)
    # Use trades from touch through decision (inclusive end).
    px = causal_price_from_trades(
        trades, decision_at=decision_at, window_start=t2, method=price_method
    )
    out: dict[str, Any] = {
        "feature_name": "near_edge_reclaim",
        "variant": "SUBMINUTE_30S",
        "window_start": t2.isoformat(),
        "window_end": decision_at.isoformat(),
        "decision_at": decision_at.isoformat(),
        "source_type": "public_trades_canonical",
        "reclaim_edge": near_edge,
        "reclaimed": None,
        "reclaimed_at": None,
        "status": "OK",
        **px,
        "max_feature_timestamp": px.get("max_source_timestamp"),
    }
    if not px["causal_ok"] or px["price"] is None:
        out["status"] = "NOT_ANALYZABLE"
        out["causal_ok"] = False
        return out
    price = float(px["price"])
    if side == "BID":
        reclaimed = price > near_edge
    else:
        reclaimed = price < near_edge
    out["reclaimed"] = bool(reclaimed)
    out["reclaimed_at"] = px["last_trade_at"] if reclaimed else None
    out["source_row_count"] = px["trade_count"]
    if px["max_source_timestamp"] and _naive_utc(px["max_source_timestamp"]) > decision_at:
        out["causal_ok"] = False
        out["status"] = "CAUSAL_VIOLATION"
    return out


def near_edge_reclaim_closed_candles(
    candles_1m: pd.DataFrame,
    *,
    side: str,
    near_edge: float,
    first_touch_at: Any,
    decision_at: pd.Timestamp,
    variant: str,
) -> dict[str, Any]:
    """Only candles whose close_time <= decision_at."""
    t2 = _naive_utc(first_touch_at)
    out: dict[str, Any] = {
        "feature_name": "near_edge_reclaim",
        "variant": variant,
        "window_start": t2.isoformat(),
        "window_end": decision_at.isoformat(),
        "decision_at": decision_at.isoformat(),
        "source_type": "candles_1m_closed",
        "reclaim_edge": near_edge,
        "reclaimed": False,
        "reclaimed_at": None,
        "price_source": "closed_1m_close",
        "price_source_timestamp": None,
        "last_trade_at": None,
        "trade_count": None,
        "source_row_count": 0,
        "missingness": None,
        "causal_ok": True,
        "max_feature_timestamp": None,
        "max_source_timestamp": None,
        "status": "OK",
    }
    if candles_1m is None or candles_1m.empty:
        out["status"] = "NOT_ANALYZABLE"
        out["missingness"] = "NO_CANDLES"
        out["causal_ok"] = False
        return out
    usable = []
    for _, r in candles_1m.iterrows():
        ot = _naive_utc(r["open_time"])
        close_t = ot + pd.Timedelta(minutes=1)
        if close_t <= decision_at and close_t > t2:
            usable.append((close_t, float(r["close"]), ot))
        elif close_t > decision_at:
            # Must not use — explicit guard
            continue
    out["source_row_count"] = len(usable)
    if not usable:
        out["status"] = "NOT_ANALYZABLE"
        out["missingness"] = "NO_CLOSED_CANDLE_BY_DECISION"
        out["causal_ok"] = False
        return out
    reclaimed = False
    reclaim_at = None
    max_ts = max(u[0] for u in usable)
    last_close = usable[-1][1]
    for close_t, close_px, _ot in usable:
        ok = close_px > near_edge if side == "BID" else close_px < near_edge
        if ok:
            reclaimed = True
            reclaim_at = close_t
            break
    out["reclaimed"] = reclaimed
    out["reclaimed_at"] = None if reclaim_at is None else reclaim_at.isoformat()
    out["price"] = last_close
    out["price_source_timestamp"] = max_ts.isoformat()
    out["max_feature_timestamp"] = max_ts.isoformat()
    out["max_source_timestamp"] = max_ts.isoformat()
    out["causal_ok"] = bool(max_ts <= decision_at)
    if not out["causal_ok"]:
        out["status"] = "CAUSAL_VIOLATION"
    return out


def absorption_subminute(
    trades: pd.DataFrame,
    *,
    side: str,
    first_touch_at: Any,
    decision_at: pd.Timestamp,
    include_pre_window: bool = True,
) -> dict[str, Any]:
    """Absorption using only trades with trade_ts <= decision_at. No post-decision candles."""
    t2 = _naive_utc(first_touch_at)
    touch = _trades_until(trades, end=decision_at, start=t2)
    pre = (
        _trades_until(trades, end=t2 - pd.Timedelta(nanoseconds=1), start=t2 - pd.Timedelta(seconds=5))
        if include_pre_window
        else pd.DataFrame()
    )
    out: dict[str, Any] = {
        "feature_name": "absorption",
        "variant": "SUBMINUTE_30S",
        "window_start": t2.isoformat(),
        "window_end": decision_at.isoformat(),
        "decision_at": decision_at.isoformat(),
        "source_type": "public_trades_canonical",
        "pre_window": "[T2-5s, T2)" if include_pre_window else None,
        "status": "OK",
        "causal_ok": True,
    }
    if touch.empty:
        out.update(
            {
                "status": "NOT_ANALYZABLE",
                "missingness": "NO_TRADES_IN_TOUCH_WINDOW",
                "causal_ok": False,
                "source_row_count": 0,
                "absorption_flag": None,
                "max_feature_timestamp": None,
                "max_source_timestamp": None,
            }
        )
        return out

    def _agg_hit(df: pd.DataFrame) -> float:
        if df.empty:
            return 0.0
        if side == "BID":
            # sells hit bids
            m = df["side"].astype(str).str.lower().isin(["sell", "s"])
            return float(df.loc[m, "notional"].sum()) if "notional" in df else float(
                (df.loc[m, "price"] * df.loc[m, "size"]).sum()
            )
        m = df["side"].astype(str).str.lower().isin(["buy", "b"])
        return float(df.loc[m, "notional"].sum()) if "notional" in df else float(
            (df.loc[m, "price"] * df.loc[m, "size"]).sum()
        )

    start_px = float(touch.iloc[0]["price"])
    end_px = float(touch.iloc[-1]["price"])
    max_ts = _naive_utc(touch.iloc[-1]["_ts"])
    # adverse excursion from trades only
    prices = touch["price"].astype(float)
    if side == "BID":
        # adverse = lower
        adverse = float(start_px - prices.min()) / start_px if start_px else 0.0
        continuation = max(0.0, (start_px - end_px) / start_px) if start_px else 0.0
    else:
        adverse = float(prices.max() - start_px) / start_px if start_px else 0.0
        continuation = max(0.0, (end_px - start_px) / start_px) if start_px else 0.0

    hit = _agg_hit(touch)
    impact = (continuation / (hit / 1e6)) if hit > 0 else None
    flag = bool(hit > 0 and continuation < ABSORPTION_CONTINUATION_MAX)

    out.update(
        {
            "trade_count": int(len(touch)),
            "source_row_count": int(len(touch)),
            "pre_trade_count": int(len(pre)),
            "agg_hit_notional": hit,
            "start_price": start_px,
            "end_price": end_px,
            "price_continuation": continuation,
            "adverse_excursion": adverse,
            "impact_per_agg": impact,
            "absorption_flag": flag,
            "price_source": "first_last_trade",
            "price_source_timestamp": max_ts.isoformat(),
            "max_feature_timestamp": max_ts.isoformat(),
            "max_source_timestamp": max_ts.isoformat(),
            "missingness": None,
            "causal_ok": bool(max_ts <= decision_at),
        }
    )
    if not out["causal_ok"]:
        out["status"] = "CAUSAL_VIOLATION"
        out["absorption_flag"] = None
    return out


def assert_causal(feature: dict[str, Any]) -> None:
    """Fail-fast: max_source_timestamp must be <= decision_at for valid features."""
    if feature.get("status") == "NOT_ANALYZABLE":
        return
    dec = _naive_utc(feature["decision_at"])
    mx = feature.get("max_source_timestamp") or feature.get("max_feature_timestamp")
    if mx is None:
        raise AssertionError(f"missing max_source_timestamp for {feature.get('feature_name')}")
    if _naive_utc(mx) > dec:
        raise AssertionError(
            f"CAUSAL_VIOLATION {feature.get('feature_name')}: max={mx} > decision_at={dec}"
        )


def future_only_path_labels(
    candles_1m: pd.DataFrame,
    *,
    side: str,
    near_edge: float,
    lower: float,
    upper: float,
    decision_at: pd.Timestamp,
    atr: float,
    horizons_min: tuple[int, ...] = (1, 3, 5, 15, 30),
) -> dict[str, Any]:
    """Path outcomes strictly after decision_at (open_time > decision_at)."""
    mid = (lower + upper) / 2.0
    path = candles_1m[candles_1m["open_time"].map(_naive_utc) > decision_at].copy()
    out: dict[str, Any] = {
        "decision_at": decision_at.isoformat(),
        "path_starts_after_decision": True,
        "atr_at_decision": atr,
    }

    def _sl(h: int) -> pd.DataFrame:
        end = decision_at + pd.Timedelta(minutes=h)
        return path[path["open_time"].map(_naive_utc) <= end]

    for h in horizons_min:
        sl = _sl(h)
        if sl.empty or not atr or atr != atr:
            out[f"hold_reclaim_{h}m"] = None
            out[f"edge_lost_{h}m"] = None
            out[f"fav_0_25atr_{h}m"] = None
            out[f"fav_0_5atr_{h}m"] = None
            out[f"fav_1_0atr_{h}m"] = None
            out[f"adv_0_25atr_{h}m"] = None
            out[f"adv_0_5atr_{h}m"] = None
            out[f"resweep_{h}m"] = None
            continue
        if side == "BID":
            hold = bool((sl["close"] > near_edge).all()) if len(sl) else None
            lost = bool((sl["close"] <= near_edge).any())
            fav025 = bool((sl["high"] >= near_edge + 0.25 * atr).any())
            fav05 = bool((sl["high"] >= near_edge + 0.5 * atr).any())
            fav10 = bool((sl["high"] >= near_edge + 1.0 * atr).any())
            adv025 = bool((sl["low"] <= near_edge - 0.25 * atr).any())
            adv05 = bool((sl["low"] <= near_edge - 0.5 * atr).any())
            resweep = bool((sl["low"] <= lower).any())
        else:
            hold = bool((sl["close"] < near_edge).all()) if len(sl) else None
            lost = bool((sl["close"] >= near_edge).any())
            fav025 = bool((sl["low"] <= near_edge - 0.25 * atr).any())
            fav05 = bool((sl["low"] <= near_edge - 0.5 * atr).any())
            fav10 = bool((sl["low"] <= near_edge - 1.0 * atr).any())
            adv025 = bool((sl["high"] >= near_edge + 0.25 * atr).any())
            adv05 = bool((sl["high"] >= near_edge + 0.5 * atr).any())
            resweep = bool((sl["high"] >= upper).any())
        out[f"hold_reclaim_{h}m"] = hold
        out[f"edge_lost_{h}m"] = lost
        out[f"fav_0_25atr_{h}m"] = fav025
        out[f"fav_0_5atr_{h}m"] = fav05
        out[f"fav_1_0atr_{h}m"] = fav10
        out[f"adv_0_25atr_{h}m"] = adv025
        out[f"adv_0_5atr_{h}m"] = adv05
        out[f"resweep_{h}m"] = resweep

    # path race labels on full post path
    def _first_fav(mult: float) -> pd.Timestamp:
        for _, r in path.iterrows():
            if side == "BID" and float(r["high"]) >= near_edge + mult * atr:
                return _naive_utc(r["open_time"])
            if side == "ASK" and float(r["low"]) <= near_edge - mult * atr:
                return _naive_utc(r["open_time"])
        return pd.NaT

    def _first_adv(mult: float) -> pd.Timestamp:
        for _, r in path.iterrows():
            if side == "BID" and float(r["low"]) <= near_edge - mult * atr:
                return _naive_utc(r["open_time"])
            if side == "ASK" and float(r["high"]) >= near_edge + mult * atr:
                return _naive_utc(r["open_time"])
        return pd.NaT

    t_f05, t_a025 = _first_fav(0.5), _first_adv(0.25)
    t_f10, t_a05 = _first_fav(1.0), _first_adv(0.5)
    out["fav0_5_before_adv0_25"] = bool(pd.notna(t_f05) and (pd.isna(t_a025) or t_f05 < t_a025))
    out["fav1_0_before_adv0_5"] = bool(pd.notna(t_f10) and (pd.isna(t_a05) or t_f10 < t_a05))
    out["return_to_mid"] = (
        bool(((path["low"] <= mid) & (path["high"] >= mid)).any()) if len(path) else None
    )
    out["consumed_accepted_after"] = out.get("resweep_30m")
    out["next_pool_reached"] = None
    out["next_pool_reached_note"] = "NOT_COMPUTED_NO_POOL_GRAPH"
    return out
