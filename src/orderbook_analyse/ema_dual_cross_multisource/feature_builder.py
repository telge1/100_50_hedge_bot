"""Multi-source feature extraction for EMA candidates (confluence-only LLD)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from ..cluster_sweep_research.cluster_adapter import active_clusters_as_of, run_lld_pools, CausalVerdict
from ..cluster_sweep_research.feature_enrichment import _window_feats
from .models import Direction
from .timeframes import bar_close as compute_bar_close, timeframe_duration


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def liquidity_confluence(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    candidate_at: datetime,
    direction: str,
    bar_index: int,
) -> dict[str, Any]:
    """LLD as confluence context only — never generates candidate."""
    out: dict[str, Any] = {"lld_role": "CONFLUENCE_ONLY"}
    lld = run_lld_pools(df.iloc[: bar_index + 1], symbol=symbol, timeframe=timeframe)
    if lld.verdict != CausalVerdict.CAUSAL_REUSABLE:
        out["lld_status"] = lld.verdict.value
        out["lld_reason"] = lld.reason
        return out
    out["lld_status"] = "VALID"
    t = _utc(candidate_at)
    as_of = pd.Timestamp(t.replace(tzinfo=None))
    if bar_index > 0:
        as_of_ts = pd.Timestamp(df.iloc[bar_index - 1]["open_time"]).to_pydatetime()
        if as_of_ts.tzinfo is None:
            as_of_ts = as_of_ts.replace(tzinfo=timezone.utc)
    else:
        as_of_ts = t
    clusters = active_clusters_as_of(lld.pools, as_of=as_of_ts, minimum_pools=1)
    close = float(df.iloc[bar_index]["close"])
    upper = [c for c in clusters if str(c.side).lower() == "upper"]
    lower = [c for c in clusters if str(c.side).lower() == "lower"]

    def nearest(clist: list, side: str) -> dict[str, Any] | None:
        if not clist:
            return None
        if side == "upper":
            above = [c for c in clist if c.low >= close]
            pick = min(above, key=lambda c: c.low - close) if above else min(clist, key=lambda c: abs(c.mid - close))
        else:
            below = [c for c in clist if c.high <= close]
            pick = max(below, key=lambda c: close - c.high) if below else min(clist, key=lambda c: abs(c.mid - close))
        dist = (pick.mid - close) / close * 100.0 if close else None
        inside = pick.low <= close <= pick.high
        return {
            "cluster_id": pick.cluster_id,
            "side": pick.side,
            "low": pick.low,
            "high": pick.high,
            "pool_count": pick.pool_count,
            "strength_mean": pick.strength_mean,
            "distance_pct": dist,
            "inside_cluster": inside,
        }

    out["nearest_upper"] = nearest(upper, "upper")
    out["nearest_lower"] = nearest(lower, "lower")
    bull = direction.upper() == Direction.BULLISH.value
    ref = out["nearest_lower"] if bull else out["nearest_upper"]
    opp = out["nearest_upper"] if bull else out["nearest_lower"]
    out["primary_cluster"] = ref
    out["opposing_cluster"] = opp
    if ref and ref.get("inside_cluster"):
        out["cluster_reclaim_or_rejection"] = "RECLAIM" if bull else "REJECTION"
    return out


def _ema_acceleration(df: pd.DataFrame, bar_index: int) -> dict[str, float | None]:
    out: dict[str, float | None] = {"ema_9_accel": None, "ema_20_accel": None}
    if bar_index < 2:
        return out
    for key, col in (("ema_9_accel", "ema_9_slope_1"), ("ema_20_accel", "ema_20_slope_1")):
        cur = df.iloc[bar_index].get(col)
        prev = df.iloc[bar_index - 1].get(col)
        if pd.notna(cur) and pd.notna(prev):
            out[key] = float(cur) - float(prev)
    return out


def _trade_flow_flip(pre15: dict[str, Any], baseline: dict[str, Any], direction: str) -> dict[str, Any]:
    out: dict[str, Any] = {"flow_flip": None, "delta_pre15": pre15.get("delta"), "delta_baseline": baseline.get("delta")}
    d_pre = pre15.get("delta")
    d_base = baseline.get("delta")
    if d_pre is None or d_base is None:
        return out
    bull = direction.upper() == Direction.BULLISH.value
    if bull:
        if d_pre > 0 and d_base <= 0:
            out["flow_flip"] = "CONFIRMING"
        elif d_pre < 0 and d_base >= 0:
            out["flow_flip"] = "CONTRADICTING"
    else:
        if d_pre < 0 and d_base >= 0:
            out["flow_flip"] = "CONFIRMING"
        elif d_pre > 0 and d_base <= 0:
            out["flow_flip"] = "CONTRADICTING"
    return out


def _ob_meta(ob_1m: pd.DataFrame | None, decision_at: datetime, stale_minutes: int = 30) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "MISSING", "freshness_minutes": None, "carried_forward_ratio": None}
    if ob_1m is None or ob_1m.empty:
        return out
    mcol = "minute" if "minute" in ob_1m.columns else "open_time"
    t = _utc(decision_at).replace(tzinfo=None)
    sl = ob_1m[pd.to_datetime(ob_1m[mcol]) <= pd.Timestamp(t)]
    if sl.empty:
        return out
    last = pd.to_datetime(sl.iloc[-1][mcol])
    freshness = (pd.Timestamp(t) - last).total_seconds() / 60.0
    out["freshness_minutes"] = float(freshness)
    out["status"] = "STALE" if freshness > stale_minutes else "VALID"
    if "carried_forward" in sl.columns:
        out["carried_forward_ratio"] = float(sl["carried_forward"].mean())
    else:
        out["carried_forward_ratio"] = "NOT_AVAILABLE"
    for col in ("ob_walls", "pulling_stacking", "ofi"):
        out[col] = "NOT_AVAILABLE"
    return out


def _oi_features(pre15: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    oi_pre = pre15.get("oi_change")
    oi_base = baseline.get("oi_change")
    out: dict[str, Any] = {"oi_change_pre15": oi_pre, "oi_change_baseline_60m": oi_base}
    if oi_pre is not None and oi_base not in (None, 0):
        out["oi_change_rel_baseline"] = float(oi_pre) / abs(float(oi_base)) if oi_base else None
    elif oi_pre is not None:
        out["oi_change_rel_baseline"] = float(oi_pre)
    else:
        out["oi_change_rel_baseline"] = None
    return out


def _liquidation_features(pre15: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    ln, sn = pre15.get("liq_long_notional") or 0, pre15.get("liq_short_notional") or 0
    bln, bsn = baseline.get("liq_long_notional") or 0, baseline.get("liq_short_notional") or 0
    total = ln + sn
    base_total = bln + bsn
    out: dict[str, Any] = {
        "total_notional_pre15": total,
        "total_notional_baseline": base_total,
        "intensity_rel_baseline": (total / base_total) if base_total else None,
    }
    return out


def _lld_features(lld: dict[str, Any], close: float) -> dict[str, Any]:
    opp = lld.get("opposing_cluster") or {}
    primary = lld.get("primary_cluster") or {}
    opp_dist = opp.get("distance_pct")
    prim_dist = primary.get("distance_pct")
    free = abs(opp_dist) if opp_dist is not None else None
    return {
        "opposing_barrier_distance_pct": opp_dist,
        "primary_cluster_distance_pct": prim_dist,
        "free_room_pct": free,
    }


def build_gate_features(
    *,
    candidate_at: datetime,
    direction: str,
    df: pd.DataFrame,
    bar_index: int,
    trades_1m: pd.DataFrame | None,
    ob_1m: pd.DataFrame | None,
    oi_1m: pd.DataFrame | None,
    liq: pd.DataFrame | None,
    symbol: str,
    timeframe: str,
    warmup_bars: int = 79,
    decision_at: datetime | None = None,
) -> dict[str, Any]:
    bar_open = _utc(candidate_at)
    bar_close_ts = _utc(decision_at) if decision_at else compute_bar_close(bar_open, timeframe)
    tf_td = timeframe_duration(timeframe)
    pre_tf_a = bar_open - tf_td

    dir_enum = Direction.BULLISH if direction.upper() == "BULLISH" else Direction.BEARISH
    win_kw = {"bar_open": bar_open, "bar_close": bar_close_ts}
    baseline = _window_feats(
        trades_1m, ob_1m, oi_1m, liq,
        bar_open - timedelta(minutes=60), bar_open, dir_enum,
        window_role="baseline", **win_kw,
    )
    pre_tf = _window_feats(
        trades_1m, ob_1m, oi_1m, liq,
        pre_tf_a, bar_open, dir_enum,
        window_role="pre", **win_kw,
    )
    cross = _window_feats(
        trades_1m, ob_1m, oi_1m, liq,
        bar_open, bar_close_ts, dir_enum,
        window_role="cross", **win_kw,
    )

    row = df.iloc[bar_index]
    atr = float(row["atr"]) if pd.notna(row.get("atr")) else None
    close = float(row["close"])
    vol_feats = {
        "atr": atr,
        "candle_range": float(row["high"]) - float(row["low"]),
        "candle_body": abs(float(row["close"]) - float(row["open"])),
        "range_atr": ((float(row["high"]) - float(row["low"])) / atr) if atr else None,
        "body_atr": (abs(float(row["close"]) - float(row["open"])) / atr) if atr else None,
        "volume": float(row.get("volume") or 0),
    }
    lld = liquidity_confluence(
        df, symbol=symbol, timeframe=timeframe, candidate_at=bar_open, direction=direction, bar_index=bar_index
    )

    frozen_feat: dict[str, Any] = {}
    if cross.get("taker_buy_ratio") is not None:
        frozen_feat["taker_buy_ratio"] = cross["taker_buy_ratio"]
    elif pre_tf.get("taker_buy_ratio") is not None:
        frozen_feat["taker_buy_ratio"] = pre_tf["taker_buy_ratio"]
    if pre_tf.get("delta") is not None and pre_tf.get("buy_notional") is not None:
        bn = pre_tf.get("buy_notional") or 0
        sn = pre_tf.get("sell_notional") or 0
        tot = bn + sn
        frozen_feat["cvd_chg_5m"] = pre_tf["delta"] / tot if tot else None
    if cross.get("delta") is not None:
        frozen_feat["cvd_chg_3m"] = cross.get("delta")
    if cross.get("imbalance_l50_mean") is not None:
        frozen_feat["imbalance_l50"] = cross["imbalance_l50_mean"]
    elif pre_tf.get("imbalance_l50_mean") is not None:
        frozen_feat["imbalance_l50"] = pre_tf["imbalance_l50_mean"]
    if bar_index >= 5:
        c0 = float(df.iloc[bar_index - 5]["close"])
        frozen_feat["ret_5m"] = (close - c0) / c0 if c0 else None
    if bar_index >= 1:
        c1 = float(df.iloc[bar_index - 1]["close"])
        frozen_feat["ret_1m"] = (close - c1) / c1 if c1 else None
    if baseline.get("trade_count"):
        frozen_feat["vol_vs_30m_mean"] = (pre_tf.get("trade_count") or 0) / max(baseline.get("trade_count") or 1, 1)
    frozen_feat["rv5_vs_prior30_med"] = vol_feats.get("range_atr")

    return {
        "timing": {
            "bar_open": bar_open.isoformat(),
            "bar_close": bar_close_ts.isoformat(),
            "decision_at": bar_close_ts.isoformat(),
            "timeframe": timeframe,
            "timeframe_minutes": int(tf_td.total_seconds() // 60),
        },
        "windows": {
            "baseline_60m": baseline,
            "pre_timeframe": pre_tf,
            "pre_15m": pre_tf,
            "cross_candle": cross,
        },
        "volatility": vol_feats,
        "liquidity_confluence": lld,
        "frozen_gate_features": frozen_feat,
        "ema_acceleration": _ema_acceleration(df, bar_index),
        "trade_flow": _trade_flow_flip(pre_tf, baseline, direction),
        "ob_meta": _ob_meta(ob_1m, bar_close_ts),
        "oi_features": _oi_features(pre_tf, baseline),
        "liquidation_features": _liquidation_features(pre_tf, baseline),
        "lld_features": _lld_features(lld, close),
        "warmup_coverage": {
            "warmup_bars_required": warmup_bars,
            "bar_index": bar_index,
            "warmup_satisfied": bar_index >= warmup_bars,
        },
    }
