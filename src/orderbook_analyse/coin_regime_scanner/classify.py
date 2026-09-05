"""Regime labels + strategy gates for COIN_REGIME_SCANNER_V1 (pure, no I/O)."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np

from .config import (
    BTC_ALIGN_THR_1H,
    RET_THR_15M,
    RET_THR_1H,
    RET_THR_4H,
    SCANNER_VERSION,
)

VolRegime = Literal["low", "normal", "high", "unknown"]
TrendRegime = Literal["bullish", "bearish", "neutral"]
RangeRegime = Literal["range", "trend", "choppy", "unknown"]
MomentumRegime = Literal["expanding", "fading", "quiet", "unknown"]
MarketAlign = Literal["aligned", "against", "neutral", "unknown"]
ObRegime = Literal["supportive", "against", "neutral", "unavailable"]
BreakoutReady = Literal["none", "watch", "active"]
GateState = Literal["allow", "watch", "block"]


def _finite(x: Any) -> bool:
    try:
        return bool(np.isfinite(float(x)))
    except (TypeError, ValueError):
        return False


def _sign(x: float, thr: float) -> int:
    if not _finite(x):
        return 0
    if x > thr:
        return 1
    if x < -thr:
        return -1
    return 0


def classify_vol(feat: dict[str, Any]) -> tuple[VolRegime, dict[str, Any]]:
    now = feat.get("rv_60m_now", feat.get("rv_60m"))
    p33 = feat.get("rv_60m_p33")
    p66 = feat.get("rv_60m_p66")
    detail = {
        "rv_15m": feat.get("rv_15m"),
        "rv_60m": feat.get("rv_60m"),
        "rv_24h": feat.get("rv_24h"),
        "rv_60m_p33": p33,
        "rv_60m_p66": p66,
    }
    if not (_finite(now) and _finite(p33) and _finite(p66)):
        return "unknown", detail
    now_f = float(now)
    if now_f <= float(p33):
        return "low", detail
    if now_f >= float(p66):
        return "high", detail
    return "normal", detail


def classify_trend(feat: dict[str, Any]) -> tuple[TrendRegime, dict[str, Any]]:
    r15 = float(feat.get("ret_15m", float("nan")))
    r1h = float(feat.get("ret_1h", float("nan")))
    r4h = float(feat.get("ret_4h", float("nan")))
    slope = float(feat.get("ema20_slope_15", float("nan")))
    votes = [
        _sign(r15, RET_THR_15M),
        _sign(r1h, RET_THR_1H),
        _sign(r4h, RET_THR_4H),
    ]
    if _finite(slope):
        votes.append(_sign(slope, RET_THR_15M))
    score = int(sum(votes))
    detail = {
        "ret_15m": r15,
        "ret_1h": r1h,
        "ret_4h": r4h,
        "ema20_slope_15": slope,
        "vote_score": score,
    }
    if score >= 2:
        return "bullish", detail
    if score <= -2:
        return "bearish", detail
    return "neutral", detail


def classify_range(feat: dict[str, Any]) -> tuple[RangeRegime, dict[str, Any]]:
    rng = feat.get("range") or {}
    detail = {
        "width": rng.get("width"),
        "width_rank": feat.get("width_rank"),
        "touches_up": rng.get("touches_up"),
        "touches_dn": rng.get("touches_dn"),
        "trend_share": rng.get("trend_share"),
        "ret_w": rng.get("ret_w"),
    }
    if not rng.get("ok"):
        return "unknown", detail
    ts = float(rng.get("trend_share", float("nan")))
    wr = float(feat.get("width_rank", float("nan")))
    up = int(rng.get("touches_up") or 0)
    dn = int(rng.get("touches_dn") or 0)
    # Strong directional move through the window → trend
    if _finite(ts) and ts >= 0.55:
        return "trend", detail
    # Mid-width with both-side touches → consolidation range
    if _finite(wr) and 0.20 <= wr <= 0.70 and up >= 2 and dn >= 2 and (_finite(ts) and ts < 0.40):
        return "range", detail
    # Narrow oscillating / many touches without direction → choppy
    if (up + dn) >= 4 and (_finite(ts) and ts < 0.35):
        return "choppy", detail
    if _finite(wr) and wr < 0.15 and (_finite(ts) and ts < 0.30):
        return "choppy", detail
    if _finite(ts) and ts >= 0.40:
        return "trend", detail
    if up >= 1 and dn >= 1 and _finite(wr) and wr <= 0.75:
        return "range", detail
    return "unknown", detail


def classify_momentum(feat: dict[str, Any], trend: TrendRegime) -> tuple[MomentumRegime, dict[str, Any]]:
    d3 = float(feat.get("delta_3m", float("nan")))
    d5 = float(feat.get("delta_5m", float("nan")))
    tps = float(feat.get("tps", float("nan")))
    r15 = float(feat.get("ret_15m", float("nan")))
    r1h = float(feat.get("ret_1h", float("nan")))
    detail = {"delta_3m": d3, "delta_5m": d5, "tps": tps, "ret_15m": r15, "ret_1h": r1h}
    if not feat.get("trades_ok", False) and not (_finite(d5) or _finite(tps)):
        # fall back to price-only momentum
        if not (_finite(r15) and _finite(r1h)):
            return "unknown", detail
        if abs(r15) >= abs(r1h) * 0.9 and abs(r15) >= RET_THR_15M:
            return "expanding", detail
        if abs(r15) < RET_THR_15M * 0.5 and abs(r1h) < RET_THR_1H * 0.5:
            return "quiet", detail
        if abs(r15) < abs(r1h) * 0.5 and abs(r1h) >= RET_THR_1H:
            return "fading", detail
        return "unknown", detail

    tps_hi = _finite(tps) and tps >= 1.0  # ~60 trades/min threshold soft
    tps_lo = (not _finite(tps)) or tps < 0.25
    flow_hi = _finite(d5) and abs(d5) > 0  # relative later; absolute presence
    # Use direction-aware expansion: flow agrees with trend/return and is larger short-term
    directed = 0
    if trend == "bullish" or (_finite(r1h) and r1h > RET_THR_1H):
        directed = 1
    elif trend == "bearish" or (_finite(r1h) and r1h < -RET_THR_1H):
        directed = -1

    if tps_lo and (not _finite(d5) or abs(d5) < 1e-12) and abs(r15) < RET_THR_15M * 0.5:
        return "quiet", detail
    if directed != 0 and _finite(d5) and _finite(d3):
        if np.sign(d5) == directed and abs(d5) >= abs(d3) and (tps_hi or abs(r15) >= RET_THR_15M):
            return "expanding", detail
        if np.sign(d5) == directed and abs(d3) < abs(d5) * 0.5:
            return "fading", detail
        if np.sign(d5) == -directed and abs(d5) > 0:
            return "fading", detail
    if tps_hi and flow_hi and _finite(r15) and abs(r15) >= RET_THR_15M:
        return "expanding", detail
    if tps_lo and abs(r15) < RET_THR_15M * 0.5:
        return "quiet", detail
    if _finite(r1h) and abs(r1h) >= RET_THR_1H and _finite(r15) and abs(r15) < abs(r1h) * 0.4:
        return "fading", detail
    return "unknown", detail


def classify_market_alignment(
    coin_feat: dict[str, Any],
    btc_feat: dict[str, Any] | None,
) -> tuple[MarketAlign, dict[str, Any]]:
    if not btc_feat:
        return "unknown", {"reason": "btc_missing"}
    c1 = float(coin_feat.get("ret_1h", float("nan")))
    b1 = float(btc_feat.get("ret_1h", float("nan")))
    c15 = float(coin_feat.get("ret_15m", float("nan")))
    b15 = float(btc_feat.get("ret_15m", float("nan")))
    detail = {"coin_ret_1h": c1, "btc_ret_1h": b1, "coin_ret_15m": c15, "btc_ret_15m": b15}
    cs = _sign(c1, BTC_ALIGN_THR_1H)
    bs = _sign(b1, BTC_ALIGN_THR_1H)
    if cs == 0 and bs == 0:
        # soft check 15m
        cs = _sign(c15, RET_THR_15M)
        bs = _sign(b15, RET_THR_15M)
        if cs == 0 and bs == 0:
            return "neutral", detail
    if cs == 0 or bs == 0:
        return "neutral", detail
    if cs == bs:
        return "aligned", detail
    return "against", detail


def classify_ob(feat: dict[str, Any], trend: TrendRegime) -> tuple[ObRegime, dict[str, Any]]:
    if not feat.get("ob_ok"):
        return "unavailable", {"reason": "ob_missing_or_thin"}
    imb = float(feat.get("imbalance_l50", float("nan")))
    ofi = float(feat.get("ofi_5m", float("nan")))
    spread = float(feat.get("spread_bps", float("nan")))
    detail = {"imbalance_l50": imb, "ofi_5m": ofi, "spread_bps": spread}
    direction = 0
    if trend == "bullish":
        direction = 1
    elif trend == "bearish":
        direction = -1
    else:
        r1h = float(feat.get("ret_1h", float("nan")))
        direction = _sign(r1h, RET_THR_1H)
    if direction == 0:
        return "neutral", detail
    votes = 0
    n = 0
    if _finite(imb):
        n += 1
        if np.sign(imb) == direction:
            votes += 1
        elif np.sign(imb) == -direction:
            votes -= 1
    if _finite(ofi):
        n += 1
        if np.sign(ofi) == direction:
            votes += 1
        elif np.sign(ofi) == -direction:
            votes -= 1
    if n == 0:
        return "unavailable", {**detail, "reason": "ob_fields_nan"}
    if votes >= 1:
        return "supportive", detail
    if votes <= -1:
        return "against", detail
    return "neutral", detail


def classify_breakout_readiness(
    *,
    range_regime: RangeRegime,
    vol_regime: VolRegime,
    market_alignment: MarketAlign,
    feat: dict[str, Any],
    momentum: MomentumRegime,
) -> tuple[BreakoutReady, dict[str, Any]]:
    rng = feat.get("range") or {}
    near = bool(rng.get("near_high") or rng.get("near_low"))
    outside = bool(rng.get("outside_high") or rng.get("outside_low"))
    detail = {
        "near_edge": near,
        "outside": outside,
        "near_high": rng.get("near_high"),
        "near_low": rng.get("near_low"),
    }
    if market_alignment == "against":
        return "none", {**detail, "reason": "market_against"}
    if range_regime != "range" and not outside:
        # allow watch also when trend_flag style near edge after range→trend ambiguity
        if range_regime not in ("range", "unknown"):
            return "none", {**detail, "reason": "not_range"}
    vol_ok = vol_regime in ("normal", "high")
    if outside and vol_ok and market_alignment != "against":
        flow_ok = momentum in ("expanding", "unknown") or feat.get("trades_ok")
        if flow_ok:
            return "active", detail
        return "watch", detail
    if range_regime == "range" and vol_ok and near and market_alignment != "against":
        return "watch", detail
    return "none", detail


def _gate(state: GateState, reasons: list[str]) -> dict[str, Any]:
    return {"state": state, "reasons": reasons}


def strategy_gates(
    *,
    vol: VolRegime,
    trend: TrendRegime,
    range_r: RangeRegime,
    momentum: MomentumRegime,
    market: MarketAlign,
    ob: ObRegime,
    breakout: BreakoutReady,
    candles_ok: bool,
) -> dict[str, Any]:
    # --- range60_breakout_ob ---
    if not candles_ok:
        r60 = _gate("block", ["candles_missing"])
    elif vol == "low" or momentum == "quiet":
        r60 = _gate("block", ["quiet_or_low_vol"])
    elif range_r == "choppy":
        r60 = _gate("block", ["choppy"])
    elif market == "against":
        r60 = _gate("block", ["market_strongly_against"])
    elif ob == "against":
        r60 = _gate("block", ["ob_against"])
    elif vol not in ("normal", "high"):
        r60 = _gate("block", ["vol_unknown_or_low"])
    elif breakout in ("watch", "active") and range_r in ("range", "trend", "unknown"):
        if breakout == "active" and ob != "against":
            r60 = _gate("allow", ["breakout_active", f"vol={vol}", f"range={range_r}", f"ob={ob}"])
        else:
            r60 = _gate("watch", ["breakout_watch", f"vol={vol}", f"range={range_r}", f"ob={ob}"])
    else:
        r60 = _gate("block", ["not_breakout_ready"])

    # --- trend_flag_breakout ---
    if not candles_ok:
        tf = _gate("block", ["candles_missing"])
    elif trend not in ("bullish", "bearish"):
        tf = _gate("block", ["no_trend"])
    elif range_r == "choppy":
        tf = _gate("block", ["choppy"])
    elif vol == "low":
        tf = _gate("block", ["low_vol"])
    elif market == "against":
        tf = _gate("block", ["market_against"])
    elif market == "aligned" and (
        momentum in ("expanding", "fading", "unknown") or trend in ("bullish", "bearish")
    ):
        if momentum == "expanding":
            tf = _gate("allow", ["trend+expanding+aligned"])
        else:
            tf = _gate("watch", ["trend+aligned", f"momentum={momentum}"])
    else:
        tf = _gate(
            "watch" if trend in ("bullish", "bearish") and vol in ("normal", "high") else "block",
            [f"trend={trend}", f"momentum={momentum}", f"market={market}"],
        )

    # absorption_reclaim filled by absorption_gate() in build_coin_regime
    return {
        "range60_breakout_ob": r60,
        "trend_flag_breakout": tf,
    }


def absorption_gate(feat: dict[str, Any], momentum: MomentumRegime, ob: ObRegime) -> dict[str, Any]:
    rng = feat.get("range") or {}
    near = bool(rng.get("near_high") or rng.get("near_low"))
    d5 = float(feat.get("delta_5m", float("nan")))
    extreme_flow = _finite(d5) and abs(d5) > 0 and (
        abs(float(feat.get("delta_3m", 0.0) or 0.0)) >= abs(d5) * 0.8
    )
    # Without explicit LLD/pool in V1, require near range edge + extreme flow + not expanding breakout
    if near and extreme_flow and momentum in ("fading", "quiet", "unknown") and ob != "unavailable":
        return _gate("watch", ["near_range_edge", "extreme_flow", "ob_present"])
    return _gate("block", ["context_only_no_level_pool_v1"])


def timeframe_slice(feat: dict[str, Any], bars: int, label: str) -> dict[str, Any]:
    """Compact TF summary using close returns over `bars` minutes."""
    key = {5: "ret_5m", 15: "ret_15m", 60: "ret_1h", 240: "ret_4h"}.get(bars)
    ret = float(feat.get(key, float("nan"))) if key else float("nan")
    # For 5m we may not have precomputed — caller can inject ret_5m
    if bars == 5:
        ret = float(feat.get("ret_5m", float("nan")))
    direction = "neutral"
    thr = {5: RET_THR_15M * 0.5, 15: RET_THR_15M, 60: RET_THR_1H, 240: RET_THR_4H}.get(bars, RET_THR_1H)
    s = _sign(ret, thr)
    if s > 0:
        direction = "bullish"
    elif s < 0:
        direction = "bearish"
    return {
        "label": label,
        "ret": ret if _finite(ret) else None,
        "direction": direction,
    }


def build_coin_regime(
    *,
    symbol: str,
    as_of: str,
    feat: dict[str, Any],
    btc_feat: dict[str, Any] | None,
    candles_ok: bool,
    ob_available: bool,
    trades_available: bool,
    missing_reason: str | None = None,
) -> dict[str, Any]:
    """Assemble the per-coin JSON contract."""
    if not candles_ok or not feat:
        return {
            "symbol": symbol,
            "as_of": as_of,
            "scanner_version": SCANNER_VERSION,
            "data_quality": {
                "candles_ok": False,
                "ob_ok": bool(ob_available),
                "trades_ok": bool(trades_available),
                "missing_reason": missing_reason or "candles_missing",
            },
            "timeframes": {"5m": {}, "15m": {}, "1h": {}, "4h": {}},
            "vol_regime": "unknown",
            "trend_regime": "neutral",
            "range_regime": "unknown",
            "momentum_regime": "unknown",
            "market_alignment": "unknown",
            "ob_regime": "unavailable",
            "breakout_readiness": "none",
            "strategy_gates": {
                "range60_breakout_ob": _gate("block", ["candles_missing"]),
                "trend_flag_breakout": _gate("block", ["candles_missing"]),
                "absorption_reclaim": _gate("block", ["candles_missing"]),
            },
            "features": {},
        }

    # inject ret_5m if provided by caller
    vol, vol_d = classify_vol(feat)
    trend, trend_d = classify_trend(feat)
    range_r, range_d = classify_range(feat)
    mom, mom_d = classify_momentum(feat, trend)
    market, market_d = classify_market_alignment(feat, btc_feat)
    ob, ob_d = classify_ob(feat, trend)
    if not ob_available:
        ob, ob_d = "unavailable", {"reason": "ob_not_loaded"}
    br, br_d = classify_breakout_readiness(
        range_regime=range_r,
        vol_regime=vol,
        market_alignment=market,
        feat=feat,
        momentum=mom,
    )
    gates = strategy_gates(
        vol=vol,
        trend=trend,
        range_r=range_r,
        momentum=mom,
        market=market,
        ob=ob,
        breakout=br,
        candles_ok=True,
    )
    gates["absorption_reclaim"] = absorption_gate(feat, mom, ob)

    def _clean(d: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in d.items():
            if isinstance(v, float) and not np.isfinite(v):
                out[k] = None
            elif isinstance(v, dict):
                out[k] = _clean(v)
            else:
                out[k] = v
        return out

    return {
        "symbol": symbol,
        "as_of": as_of,
        "scanner_version": SCANNER_VERSION,
        "data_quality": {
            "candles_ok": True,
            "ob_ok": bool(ob_available and feat.get("ob_ok")),
            "trades_ok": bool(trades_available and feat.get("trades_ok")),
            "missing_reason": missing_reason,
        },
        "timeframes": {
            "5m": timeframe_slice(feat, 5, "5m"),
            "15m": timeframe_slice(feat, 15, "15m"),
            "1h": timeframe_slice(feat, 60, "1h"),
            "4h": timeframe_slice(feat, 240, "4h"),
        },
        "vol_regime": vol,
        "trend_regime": trend,
        "range_regime": range_r,
        "momentum_regime": mom,
        "market_alignment": market,
        "ob_regime": ob,
        "breakout_readiness": br,
        "strategy_gates": {
            "range60_breakout_ob": gates["range60_breakout_ob"],
            "trend_flag_breakout": gates["trend_flag_breakout"],
            "absorption_reclaim": gates["absorption_reclaim"],
        },
        "features": _clean(
            {
                "vol": vol_d,
                "trend": trend_d,
                "range": range_d,
                "momentum": mom_d,
                "market": market_d,
                "ob": ob_d,
                "breakout": br_d,
                "close": feat.get("close"),
                "n_bars": feat.get("n_bars"),
            }
        ),
    }
