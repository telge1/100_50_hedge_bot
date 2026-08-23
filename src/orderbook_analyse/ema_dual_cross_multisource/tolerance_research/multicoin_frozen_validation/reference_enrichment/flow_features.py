"""Public trade / flow features (causal to decision_at); direction-mirrored."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .causality import as_utc, mirror_for_direction, rows_at_or_before, window_slice
from .feature_value import FeatureValue, missing, ok

SRC = "orderbook_analysis.public_trades_canonical"


def _agg_from_minutes(trades_1m: pd.DataFrame, decision_at: datetime, minutes: int) -> dict[str, float | None]:
    """Aggregate pre-binned 1m trades with columns minute, buy_notional, sell_notional, trade_count."""
    sl = window_slice(trades_1m, time_col="minute", end=decision_at, lookback=timedelta(minutes=minutes))
    if sl.empty:
        return {
            "buy": None,
            "sell": None,
            "total": None,
            "count": None,
            "empty": True,
            "w0": None,
        }
    buy = float(pd.to_numeric(sl["buy_notional"], errors="coerce").fillna(0).sum())
    sell = float(pd.to_numeric(sl["sell_notional"], errors="coerce").fillna(0).sum())
    cnt = int(pd.to_numeric(sl["trade_count"], errors="coerce").fillna(0).sum())
    return {"buy": buy, "sell": sell, "total": buy + sell, "count": cnt, "empty": False, "w0": sl.iloc[0]["minute"]}


def _agg_from_ticks(trades: pd.DataFrame, decision_at: datetime, minutes: int, time_col: str) -> dict[str, float | None]:
    sl = window_slice(trades, time_col=time_col, end=decision_at, lookback=timedelta(minutes=minutes))
    if sl.empty:
        return {"buy": None, "sell": None, "total": None, "count": None, "empty": True, "w0": None}
    side = sl["side"].astype(str).str.upper()
    notional = pd.to_numeric(sl["size"], errors="coerce") * pd.to_numeric(sl["price"], errors="coerce")
    buy = float(notional.loc[side.isin(("BUY", "B"))].sum())
    sell = float(notional.loc[side.isin(("SELL", "S"))].sum())
    return {
        "buy": buy,
        "sell": sell,
        "total": buy + sell,
        "count": int(len(sl)),
        "empty": False,
        "w0": sl.iloc[0][time_col],
    }


def compute_flow_features(
    trades: pd.DataFrame | None,
    decision_at: datetime | str,
    direction: str,
) -> dict[str, FeatureValue]:
    dec = as_utc(decision_at)
    names_base = [
        "buy_volume_1m",
        "sell_volume_1m",
        "buy_volume_5m",
        "sell_volume_5m",
        "total_volume_1m",
        "total_volume_5m",
        "trade_count_1m",
        "trade_count_5m",
        "buy_ratio_1m",
        "buy_ratio_5m",
        "signed_flow_1m",
        "signed_flow_5m",
        "directional_flow_1m",
        "directional_flow_5m",
        "flow_acceleration",
    ]
    if trades is None or trades.empty:
        return {n: missing(n, reason="NO_TRADES", status="MISSING", source=SRC, asof=dec) for n in names_base}

    use_minutes = "minute" in trades.columns and "buy_notional" in trades.columns
    if use_minutes:
        causal = rows_at_or_before(trades, dec, time_col="minute")
        agg_fn = lambda m: _agg_from_minutes(causal, dec, m)
    else:
        tcol = "trade_ts" if "trade_ts" in trades.columns else None
        if tcol is None:
            return {n: missing(n, reason="NO_TIME_COLUMN", status="MISSING", source=SRC, asof=dec) for n in names_base}
        causal = rows_at_or_before(trades, dec, time_col=tcol)
        agg_fn = lambda m: _agg_from_ticks(causal, dec, m, tcol)

    if causal.empty:
        return {n: missing(n, reason="NO_CAUSAL_TRADES", status="MISSING", source=SRC, asof=dec) for n in names_base}

    a1 = agg_fn(1)
    a5 = agg_fn(5)
    feats: dict[str, FeatureValue] = {}

    def emit_vol(prefix: str, agg: dict, minutes: int):
        if agg["empty"]:
            for n in (f"buy_volume_{prefix}", f"sell_volume_{prefix}", f"total_volume_{prefix}", f"trade_count_{prefix}"):
                feats[n] = missing(n, reason="EMPTY_WINDOW", status="MISSING", source=SRC, asof=dec)
            feats[f"buy_ratio_{prefix}"] = missing(f"buy_ratio_{prefix}", reason="EMPTY_WINDOW", status="MISSING", source=SRC, asof=dec)
            feats[f"signed_flow_{prefix}"] = missing(f"signed_flow_{prefix}", reason="EMPTY_WINDOW", status="MISSING", source=SRC, asof=dec)
            feats[f"directional_flow_{prefix}"] = missing(
                f"directional_flow_{prefix}", reason="EMPTY_WINDOW", status="MISSING", source=SRC, asof=dec
            )
            return
        w0, w1 = agg["w0"], dec
        feats[f"buy_volume_{prefix}"] = ok(f"buy_volume_{prefix}", agg["buy"], asof=dec, window_start=w0, window_end=w1, source=SRC)
        feats[f"sell_volume_{prefix}"] = ok(f"sell_volume_{prefix}", agg["sell"], asof=dec, window_start=w0, window_end=w1, source=SRC)
        feats[f"total_volume_{prefix}"] = ok(f"total_volume_{prefix}", agg["total"], asof=dec, window_start=w0, window_end=w1, source=SRC)
        feats[f"trade_count_{prefix}"] = ok(f"trade_count_{prefix}", agg["count"], asof=dec, window_start=w0, window_end=w1, source=SRC)
        if agg["total"] and agg["total"] > 0:
            ratio = agg["buy"] / agg["total"]
            feats[f"buy_ratio_{prefix}"] = ok(f"buy_ratio_{prefix}", ratio, asof=dec, window_start=w0, window_end=w1, source=SRC)
        else:
            feats[f"buy_ratio_{prefix}"] = missing(f"buy_ratio_{prefix}", reason="ZERO_TOTAL", status="MISSING", source=SRC, asof=dec)
        signed = agg["buy"] - agg["sell"]
        feats[f"signed_flow_{prefix}"] = ok(f"signed_flow_{prefix}", signed, asof=dec, window_start=w0, window_end=w1, source=SRC)
        feats[f"directional_flow_{prefix}"] = ok(
            f"directional_flow_{prefix}",
            mirror_for_direction(signed, direction),
            asof=dec,
            window_start=w0,
            window_end=w1,
            source=SRC,
        )

    emit_vol("1m", a1, 1)
    emit_vol("5m", a5, 5)

    # Acceleration: directional 1m flow minus mean of prior 4 minutes of 1m signed flow (causal)
    # Using 5m aggregate vs 1m: (directional_1m) - (directional_5m - directional_1m)/4 when possible
    d1 = feats.get("directional_flow_1m")
    d5 = feats.get("directional_flow_5m")
    if d1 and d5 and d1.value is not None and d5.value is not None:
        prior = (float(d5.value) - float(d1.value)) / 4.0
        accel = float(d1.value) - prior
        feats["flow_acceleration"] = ok("flow_acceleration", accel, asof=dec, window_start=a5.get("w0"), window_end=dec, source=SRC)
    else:
        feats["flow_acceleration"] = missing("flow_acceleration", reason="NEED_1M_AND_5M", status="INSUFFICIENT", source=SRC, asof=dec)
    return feats
