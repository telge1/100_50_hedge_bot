"""Research-only 5m Breakdown / Risk-Off state machine (counterfactual).

Separate from regime classification and from the B3 Strong-Trend Direction Gate.
Disabled by default (``RiskOffConfig.enabled=False``). Outcomes must never enter
state computation — join them only after the timeline is complete.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.config import RegimeScannerConfig, default_regime_scanner_config
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.structure import classify_swing_structure
from research.regime_scanner.swings import ConfirmedPivot, find_confirmed_pivots

RiskVariant = Literal["R1", "R2", "R3", "R4"]
RiskState = Literal[
    "normal",
    "long_risk_elevated",
    "long_risk_off",
    "short_risk_elevated",
    "short_risk_off",
    "covered_by_strong_bearish",
    "covered_by_strong_bullish",
    "unavailable",
]
BlockingLayer = Literal["none", "b3", "risk_off", "risk_elevated", "covered"]

EMA20_SLOPE_KEY = "ema_20_slope_12_pct"
EMA9_SLOPE_KEY = "ema_9_slope_3_pct"
EMA59_SLOPE_KEY = "ema_59_slope_48_pct"


@dataclass(frozen=True)
class RiskOffConfig:
    """Configurable Breakdown / Risk-Off gate (research only)."""

    enabled: bool = False
    variant: RiskVariant = "R1"
    min_hold_bars: int = 3
    elevated_score: float = 3.0
    off_score: float = 5.0
    exit_score: float = 1.5
    exit_below_bars: int = 2
    atr_impulse_strong: float = 1.0
    cum_ret_2_thresh: float = -0.35
    cum_ret_3_thresh: float = -0.55
    cum_ret_4_thresh: float = -0.75
    near_high_tol_pct: float = 0.15
    upper_wick_ratio: float = 0.45
    volume_bear_ratio: float = 1.15
    vol_median_window: int = 20
    structure_epsilon_pct: float = 0.01
    confirm_candles_normal: int = 2
    confirm_candles_elevated: int = 3


def default_risk_off_config(*, variant: RiskVariant = "R1") -> RiskOffConfig:
    return RiskOffConfig(variant=variant, enabled=False)


def _finite(v: object) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def _to_utc(ts: object) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def session_extrema_as_of(
    highs: Sequence[float] | np.ndarray | pd.Series,
    lows: Sequence[float] | np.ndarray | pd.Series,
    timestamps: Sequence[object] | np.ndarray | pd.Series,
    i: int,
) -> tuple[float | None, float | None]:
    """UTC-day session high/low using bars ``0..i`` only (causal)."""
    if i < 0:
        return None, None
    day = _to_utc(timestamps[i]).normalize()
    hi_vals: list[float] = []
    lo_vals: list[float] = []
    for j in range(0, i + 1):
        if _to_utc(timestamps[j]).normalize() != day:
            continue
        h = _finite(highs[j])
        l = _finite(lows[j])
        if h is not None:
            hi_vals.append(h)
        if l is not None:
            lo_vals.append(l)
    session_high = max(hi_vals) if hi_vals else None
    session_low = min(lo_vals) if lo_vals else None
    return session_high, session_low


def prior_day_extrema(
    highs: Sequence[float] | np.ndarray | pd.Series,
    lows: Sequence[float] | np.ndarray | pd.Series,
    timestamps: Sequence[object] | np.ndarray | pd.Series,
    i: int,
) -> tuple[float | None, float | None]:
    """Prior UTC calendar-day high/low only after that day is fully complete.

    Available only when bar ``i`` belongs to a later UTC day than the prior day,
    so the prior day's session is finished.
    """
    if i < 0:
        return None, None
    cur_day = _to_utc(timestamps[i]).normalize()
    prior_day = cur_day - pd.Timedelta(days=1)
    hi_vals: list[float] = []
    lo_vals: list[float] = []
    for j in range(0, i + 1):
        d = _to_utc(timestamps[j]).normalize()
        if d != prior_day:
            continue
        h = _finite(highs[j])
        l = _finite(lows[j])
        if h is not None:
            hi_vals.append(h)
        if l is not None:
            lo_vals.append(l)
    if not hi_vals and not lo_vals:
        return None, None
    return (max(hi_vals) if hi_vals else None, min(lo_vals) if lo_vals else None)


def _pivots_as_of(pivots: list[ConfirmedPivot], bar_index: int) -> list[ConfirmedPivot]:
    return [p for p in pivots if int(p.confirmation_index) <= int(bar_index)]


def _latest_same_side(pivots: list[ConfirmedPivot], side: str, count: int = 2) -> list[ConfirmedPivot]:
    matched = [p for p in pivots if p.pivot_type == side]
    return matched[-count:] if matched else []


def _pct_ret(close_now: float | None, close_prev: float | None) -> float | None:
    if close_now is None or close_prev is None or close_prev == 0.0:
        return None
    return (close_now - close_prev) / abs(close_prev) * 100.0


def _near_level(price: float | None, level: float | None, tol_pct: float) -> bool:
    if price is None or level is None or level == 0.0:
        return False
    return abs(price - level) / abs(level) * 100.0 <= float(tol_pct)


def _wick_ratios(
    open_: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
) -> tuple[float | None, float | None, float | None]:
    if open_ is None or high is None or low is None or close is None:
        return None, None, None
    rng = high - low
    if rng <= 0.0:
        return 0.0, 0.0, 0.0
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    return upper / rng, lower / rng, rng


@dataclass
class BarSignals:
    """Transparent per-bar Risk-Off inputs (no outcomes)."""

    bar_index: int
    bar_open: str
    decision_time: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    ret_1: float | None = None
    ret_2: float | None = None
    ret_3: float | None = None
    ret_4: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    atr_norm_ret_1: float | None = None
    ema_9: float | None = None
    ema_20: float | None = None
    ema_59: float | None = None
    ema_200: float | None = None
    ema9_slope: float | None = None
    ema20_slope: float | None = None
    ema59_slope: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None
    adx: float | None = None
    di_spread: float | None = None
    session_high: float | None = None
    session_low: float | None = None
    prior_day_high: float | None = None
    prior_day_low: float | None = None
    last_swing_high: float | None = None
    last_swing_low: float | None = None
    last_higher_low_price: float | None = None
    last_lower_high_price: float | None = None
    structure_high: str | None = None
    structure_low: str | None = None
    lower_high: bool = False
    lower_low: bool = False
    higher_high: bool = False
    higher_low: bool = False
    break_below_swing_low: bool = False
    break_above_swing_high: bool = False
    break_below_hl: bool = False
    break_above_lh: bool = False
    break_prior_day_high: bool = False
    break_prior_day_low: bool = False
    near_session_high: bool = False
    near_prior_day_high: bool = False
    near_session_low: bool = False
    near_prior_day_low: bool = False
    failed_breakout: bool = False
    failed_breakdown: bool = False
    range_reentry_from_high: bool = False
    range_reentry_from_low: bool = False
    upper_wick_rejection: bool = False
    lower_wick_rejection: bool = False
    close_lt_ema9: bool = False
    close_gt_ema9: bool = False
    close_lt_ema20: bool = False
    close_gt_ema20: bool = False
    ema9_lt_ema20: bool = False
    ema9_gt_ema20: bool = False
    ema20_slope_neg: bool = False
    ema20_slope_pos: bool = False
    ema9_slope_neg: bool = False
    ema9_slope_pos: bool = False
    di_bearish: bool = False
    di_bullish: bool = False
    atr_impulse_bear: bool = False
    atr_impulse_bull: bool = False
    cum_down_2: bool = False
    cum_down_3: bool = False
    cum_down_4: bool = False
    cum_up_2: bool = False
    cum_up_3: bool = False
    cum_up_4: bool = False
    volume_bear_expand: bool = False
    volume_bull_expand: bool = False
    bearish_candle: bool = False
    bullish_candle: bool = False
    vol_median: float | None = None
    recent_breakout_level: float | None = None
    recent_breakdown_level: float | None = None
    warmup_ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_bar_signals(
    frame: pd.DataFrame,
    i: int,
    pivots: list[ConfirmedPivot],
    cfg: RiskOffConfig,
    scanner_cfg: RegimeScannerConfig,
    vol_median: float | None,
    recent_breakout_level: float | None,
    *,
    recent_breakdown_level: float | None = None,
) -> BarSignals:
    """Build causal bar signals at closed 5m index ``i`` (decision = open + 5m)."""
    row = frame.iloc[i]
    ts = _to_utc(row["timestamp"])
    decision_time = ts + pd.Timedelta(minutes=5)

    open_ = _finite(row.get("open"))
    high = _finite(row.get("high"))
    low = _finite(row.get("low"))
    close = _finite(row.get("close"))
    volume = _finite(row.get("volume")) if "volume" in frame.columns else None
    atr = _finite(row.get("atr"))
    atr_pct = _finite(row.get("atr_pct"))
    ema9 = _finite(row.get("ema_9"))
    ema20 = _finite(row.get("ema_20"))
    ema59 = _finite(row.get("ema_59"))
    ema200 = _finite(row.get("ema_200"))
    slope9 = _finite(row.get(EMA9_SLOPE_KEY))
    slope20 = _finite(row.get(EMA20_SLOPE_KEY))
    slope59 = _finite(row.get(EMA59_SLOPE_KEY))
    plus_di = _finite(row.get("plus_di"))
    minus_di = _finite(row.get("minus_di"))
    adx = _finite(row.get("adx"))
    di_spread = _finite(row.get("di_spread"))
    if di_spread is None and plus_di is not None and minus_di is not None:
        di_spread = plus_di - minus_di

    closes = frame["close"]
    c0 = close
    c1 = _finite(closes.iloc[i - 1]) if i >= 1 else None
    c2 = _finite(closes.iloc[i - 2]) if i >= 2 else None
    c3 = _finite(closes.iloc[i - 3]) if i >= 3 else None
    c4 = _finite(closes.iloc[i - 4]) if i >= 4 else None
    ret_1 = _pct_ret(c0, c1)
    ret_2 = _pct_ret(c0, c2)
    ret_3 = _pct_ret(c0, c3)
    ret_4 = _pct_ret(c0, c4)
    atr_norm = None
    if ret_1 is not None and atr is not None and atr > 0.0 and c1 is not None and c1 != 0.0:
        # Convert %-return to ATR units: (Δclose / atr)
        atr_norm = ((c0 - c1) / atr) if c0 is not None else None

    highs = frame["high"].to_numpy()
    lows = frame["low"].to_numpy()
    timestamps = frame["timestamp"].to_numpy()
    session_high, session_low = session_extrema_as_of(highs, lows, timestamps, i)
    prior_high, prior_low = prior_day_extrema(highs, lows, timestamps, i)

    asof = _pivots_as_of(pivots, i)
    high_pivots = _latest_same_side(asof, "high", 2)
    low_pivots = _latest_same_side(asof, "low", 2)
    last_sh = high_pivots[-1] if high_pivots else None
    last_sl = low_pivots[-1] if low_pivots else None

    structure_high = structure_low = None
    lower_high = lower_low = higher_high = higher_low = False
    if len(high_pivots) == 2:
        st = classify_swing_structure(
            high_pivots[0].price,
            high_pivots[1].price,
            side="high",
            epsilon_pct=cfg.structure_epsilon_pct,
        )
        structure_high = str(st["structure_type"])
        lower_high = structure_high == "lower_high"
        higher_high = structure_high == "higher_high"
    if len(low_pivots) == 2:
        st = classify_swing_structure(
            low_pivots[0].price,
            low_pivots[1].price,
            side="low",
            epsilon_pct=cfg.structure_epsilon_pct,
        )
        structure_low = str(st["structure_type"])
        lower_low = structure_low == "lower_low"
        higher_low = structure_low == "higher_low"

    # Last confirmed HL / LH prices among confirmed pivots as of i.
    last_hl_price: float | None = None
    last_lh_price: float | None = None
    lows_all = [p for p in asof if p.pivot_type == "low"]
    highs_all = [p for p in asof if p.pivot_type == "high"]
    for k in range(1, len(lows_all)):
        st = classify_swing_structure(
            lows_all[k - 1].price,
            lows_all[k].price,
            side="low",
            epsilon_pct=cfg.structure_epsilon_pct,
        )
        if st["structure_type"] == "higher_low":
            last_hl_price = float(lows_all[k].price)
    for k in range(1, len(highs_all)):
        st = classify_swing_structure(
            highs_all[k - 1].price,
            highs_all[k].price,
            side="high",
            epsilon_pct=cfg.structure_epsilon_pct,
        )
        if st["structure_type"] == "lower_high":
            last_lh_price = float(highs_all[k].price)

    break_below_swing_low = bool(
        close is not None and last_sl is not None and close < float(last_sl.price)
    )
    break_above_swing_high = bool(
        close is not None and last_sh is not None and close > float(last_sh.price)
    )
    break_below_hl = bool(
        close is not None and last_hl_price is not None and close < float(last_hl_price)
    )
    break_above_lh = bool(
        close is not None and last_lh_price is not None and close > float(last_lh_price)
    )
    break_pdh = bool(
        close is not None and prior_high is not None and close > float(prior_high)
    )
    break_pdl = bool(
        close is not None and prior_low is not None and close < float(prior_low)
    )

    near_sh = _near_level(high, session_high, cfg.near_high_tol_pct) or _near_level(
        close, session_high, cfg.near_high_tol_pct
    )
    near_pdh = _near_level(high, prior_high, cfg.near_high_tol_pct) or _near_level(
        close, prior_high, cfg.near_high_tol_pct
    )
    near_sl = _near_level(low, session_low, cfg.near_high_tol_pct) or _near_level(
        close, session_low, cfg.near_high_tol_pct
    )
    near_pdl = _near_level(low, prior_low, cfg.near_high_tol_pct) or _near_level(
        close, prior_low, cfg.near_high_tol_pct
    )

    upper_wr, lower_wr, _rng = _wick_ratios(open_, high, low, close)
    upper_wick_rejection = bool(
        upper_wr is not None and upper_wr >= cfg.upper_wick_ratio and (near_sh or near_pdh)
    )
    lower_wick_rejection = bool(
        lower_wr is not None and lower_wr >= cfg.upper_wick_ratio and (near_sl or near_pdl)
    )

    # Failed breakout: previously tagged breakout level, close back below it.
    range_reentry_from_high = False
    failed_breakout = False
    if recent_breakout_level is not None and close is not None:
        if close < float(recent_breakout_level):
            range_reentry_from_high = True
            failed_breakout = True
    # Also treat wick rejection near high + close back under session high as failed BO.
    if (
        not failed_breakout
        and session_high is not None
        and close is not None
        and high is not None
        and near_sh
        and close < float(session_high)
        and (upper_wick_rejection or (open_ is not None and close < open_))
    ):
        # Only if this bar traded at/through the extreme then closed back inside.
        if high >= float(session_high) * (1.0 - cfg.near_high_tol_pct / 100.0):
            failed_breakout = True
            range_reentry_from_high = True

    range_reentry_from_low = False
    failed_breakdown = False
    if recent_breakdown_level is not None and close is not None:
        if close > float(recent_breakdown_level):
            range_reentry_from_low = True
            failed_breakdown = True
    if (
        not failed_breakdown
        and session_low is not None
        and close is not None
        and low is not None
        and near_sl
        and close > float(session_low)
        and (lower_wick_rejection or (open_ is not None and close > open_))
    ):
        if low <= float(session_low) * (1.0 + cfg.near_high_tol_pct / 100.0):
            failed_breakdown = True
            range_reentry_from_low = True

    bearish_candle = bool(open_ is not None and close is not None and close < open_)
    bullish_candle = bool(open_ is not None and close is not None and close > open_)
    volume_bear_expand = bool(
        bearish_candle
        and volume is not None
        and vol_median is not None
        and vol_median > 0.0
        and volume >= vol_median * cfg.volume_bear_ratio
    )
    volume_bull_expand = bool(
        bullish_candle
        and volume is not None
        and vol_median is not None
        and vol_median > 0.0
        and volume >= vol_median * cfg.volume_bear_ratio
    )

    atr_impulse_bear = bool(
        atr_norm is not None and atr_norm <= -float(cfg.atr_impulse_strong)
    )
    atr_impulse_bull = bool(
        atr_norm is not None and atr_norm >= float(cfg.atr_impulse_strong)
    )
    cum_down_2 = bool(ret_2 is not None and ret_2 <= cfg.cum_ret_2_thresh)
    cum_down_3 = bool(ret_3 is not None and ret_3 <= cfg.cum_ret_3_thresh)
    cum_down_4 = bool(ret_4 is not None and ret_4 <= cfg.cum_ret_4_thresh)
    cum_up_2 = bool(ret_2 is not None and ret_2 >= -cfg.cum_ret_2_thresh)
    cum_up_3 = bool(ret_3 is not None and ret_3 >= -cfg.cum_ret_3_thresh)
    cum_up_4 = bool(ret_4 is not None and ret_4 >= -cfg.cum_ret_4_thresh)

    warmup_ok = i + 1 >= int(scanner_cfg.min_warmup_candles)

    return BarSignals(
        bar_index=i,
        bar_open=ts.isoformat(),
        decision_time=decision_time.isoformat(),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        ret_1=ret_1,
        ret_2=ret_2,
        ret_3=ret_3,
        ret_4=ret_4,
        atr=atr,
        atr_pct=atr_pct,
        atr_norm_ret_1=atr_norm,
        ema_9=ema9,
        ema_20=ema20,
        ema_59=ema59,
        ema_200=ema200,
        ema9_slope=slope9,
        ema20_slope=slope20,
        ema59_slope=slope59,
        plus_di=plus_di,
        minus_di=minus_di,
        adx=adx,
        di_spread=di_spread,
        session_high=session_high,
        session_low=session_low,
        prior_day_high=prior_high,
        prior_day_low=prior_low,
        last_swing_high=float(last_sh.price) if last_sh else None,
        last_swing_low=float(last_sl.price) if last_sl else None,
        last_higher_low_price=last_hl_price,
        last_lower_high_price=last_lh_price,
        structure_high=structure_high,
        structure_low=structure_low,
        lower_high=lower_high,
        lower_low=lower_low,
        higher_high=higher_high,
        higher_low=higher_low,
        break_below_swing_low=break_below_swing_low,
        break_above_swing_high=break_above_swing_high,
        break_below_hl=break_below_hl,
        break_above_lh=break_above_lh,
        break_prior_day_high=break_pdh,
        break_prior_day_low=break_pdl,
        near_session_high=near_sh,
        near_prior_day_high=near_pdh,
        near_session_low=near_sl,
        near_prior_day_low=near_pdl,
        failed_breakout=failed_breakout,
        failed_breakdown=failed_breakdown,
        range_reentry_from_high=range_reentry_from_high,
        range_reentry_from_low=range_reentry_from_low,
        upper_wick_rejection=upper_wick_rejection,
        lower_wick_rejection=lower_wick_rejection,
        close_lt_ema9=bool(close is not None and ema9 is not None and close < ema9),
        close_gt_ema9=bool(close is not None and ema9 is not None and close > ema9),
        close_lt_ema20=bool(close is not None and ema20 is not None and close < ema20),
        close_gt_ema20=bool(close is not None and ema20 is not None and close > ema20),
        ema9_lt_ema20=bool(ema9 is not None and ema20 is not None and ema9 < ema20),
        ema9_gt_ema20=bool(ema9 is not None and ema20 is not None and ema9 > ema20),
        ema20_slope_neg=bool(slope20 is not None and slope20 < 0),
        ema20_slope_pos=bool(slope20 is not None and slope20 > 0),
        ema9_slope_neg=bool(slope9 is not None and slope9 < 0),
        ema9_slope_pos=bool(slope9 is not None and slope9 > 0),
        di_bearish=bool(minus_di is not None and plus_di is not None and minus_di > plus_di),
        di_bullish=bool(plus_di is not None and minus_di is not None and plus_di > minus_di),
        atr_impulse_bear=atr_impulse_bear,
        atr_impulse_bull=atr_impulse_bull,
        cum_down_2=cum_down_2,
        cum_down_3=cum_down_3,
        cum_down_4=cum_down_4,
        cum_up_2=cum_up_2,
        cum_up_3=cum_up_3,
        cum_up_4=cum_up_4,
        volume_bear_expand=volume_bear_expand,
        volume_bull_expand=volume_bull_expand,
        bearish_candle=bearish_candle,
        bullish_candle=bullish_candle,
        vol_median=vol_median,
        recent_breakout_level=recent_breakout_level,
        recent_breakdown_level=recent_breakdown_level,
        warmup_ok=warmup_ok,
    )


def _regime_15m_bearish_context(regime_15m: object | None) -> dict[str, bool]:
    s = str(regime_15m or "").lower()
    return {
        "regime_bearish": "bear" in s,
        "regime_bullish": "bull" in s,
        "regime_weakness": "weak" in s,
        "regime_transition": "transition" in s,
        "regime_strong_bearish": "strong_bear" in s,
        "regime_strong_bullish": "strong_bull" in s,
    }


def score_long_risk(
    sig: BarSignals,
    cfg: RiskOffConfig,
    regime_15m: object | None = None,
) -> dict[str, Any]:
    """Score long-side risk. Returns score / elevated / off / hard_off / components / reason."""
    empty = {
        "score": 0.0,
        "elevated": False,
        "off": False,
        "hard_off": False,
        "components": {},
        "reason": "warmup_insufficient",
    }
    if not sig.warmup_ok:
        return empty

    variant = cfg.variant
    ctx = _regime_15m_bearish_context(regime_15m)
    components: dict[str, float] = {}
    hard_off = False
    reasons: list[str] = []

    structure_break = bool(
        sig.lower_high and (sig.break_below_hl or sig.break_below_swing_low)
    )
    weak_mom = bool(sig.close_lt_ema20 or sig.ema20_slope_neg or sig.ema9_lt_ema20)
    failed_pack = bool(
        (sig.failed_breakout or sig.upper_wick_rejection)
        and sig.range_reentry_from_high
        and weak_mom
    )

    medium_flags = {
        "ema9_lt_ema20": sig.ema9_lt_ema20,
        "close_lt_ema20": sig.close_lt_ema20,
        "di_bearish": sig.di_bearish,
        "cum_down_2": sig.cum_down_2,
        "volume_bear_expand": sig.volume_bear_expand,
        "ema20_slope_neg": sig.ema20_slope_neg,
        "ema9_slope_neg": sig.ema9_slope_neg,
        "bearish_candle": sig.bearish_candle and sig.cum_down_2,
    }
    medium_count = sum(1 for v in medium_flags.values() if v)

    if variant == "R1":
        if structure_break and (sig.close_lt_ema20 or sig.ema20_slope_neg):
            hard_off = True
            components["structure_break"] = float(cfg.off_score)
            reasons.append("R1_structure_break")
        else:
            if sig.lower_high:
                components["lower_high"] = 1.0
            if sig.break_below_hl or sig.break_below_swing_low:
                components["break_below_hl_or_swing"] = 1.5
            if sig.close_lt_ema20 or sig.ema20_slope_neg:
                components["ema20_weak"] = 1.0
    elif variant == "R2":
        if failed_pack:
            hard_off = True
            components["failed_breakout_pack"] = float(cfg.off_score)
            reasons.append("R2_failed_breakout")
        else:
            if sig.failed_breakout or sig.upper_wick_rejection:
                components["failed_or_rejection"] = 2.0
            if sig.range_reentry_from_high:
                components["range_reentry"] = 1.5
            if weak_mom:
                components["weak_momentum"] = 1.5
            if sig.near_session_high or sig.near_prior_day_high:
                components["near_high"] = 0.5
    elif variant == "R3":
        if sig.atr_impulse_bear:
            hard_off = True
            components["atr_impulse"] = float(cfg.off_score)
            reasons.append("R3_atr_impulse")
        elif sig.cum_down_3 and sig.volume_bear_expand:
            hard_off = True
            components["cum3_bear_vol"] = float(cfg.off_score)
            reasons.append("R3_cum_down_bear_vol")
        elif medium_count >= 3:
            hard_off = True
            components["medium_flags"] = float(cfg.off_score)
            reasons.append("R3_medium_flags")
        else:
            if sig.atr_impulse_bear:
                components["atr_impulse"] = 3.0
            if sig.cum_down_2:
                components["cum_down_2"] = 1.0
            if sig.cum_down_3:
                components["cum_down_3"] = 1.5
            if sig.cum_down_4:
                components["cum_down_4"] = 2.0
            if sig.volume_bear_expand:
                components["volume_bear"] = 1.0
            components["medium_count"] = float(medium_count)
    elif variant == "R4":
        if structure_break:
            components["structure"] = 2.5
        if failed_pack or sig.failed_breakout:
            components["failed"] = 2.0
        if sig.upper_wick_rejection:
            components["wick_reject"] = 1.0
        if sig.atr_impulse_bear:
            components["impulse"] = 2.0
        if sig.cum_down_2:
            components["cum2"] = 0.75
        if sig.cum_down_3:
            components["cum3"] = 1.0
        if sig.cum_down_4:
            components["cum4"] = 1.25
        if sig.ema9_lt_ema20:
            components["ema_cross"] = 0.75
        if sig.close_lt_ema20:
            components["close_lt_ema20"] = 0.75
        if sig.ema20_slope_neg:
            components["ema20_slope"] = 0.5
        if sig.di_bearish:
            components["di_bearish"] = 0.75
        if sig.volume_bear_expand:
            components["vol_bear"] = 0.75
        if ctx["regime_bearish"] or ctx["regime_weakness"] or ctx["regime_transition"]:
            components["ctx_15m"] = 1.0
        if ctx["regime_strong_bearish"]:
            components["ctx_15m_strong"] = 1.5
        if structure_break and sig.atr_impulse_bear:
            hard_off = True
            reasons.append("R4_structure_plus_impulse")
    else:
        raise ValueError(f"unknown variant {variant}")

    score = float(sum(components.values()))
    if hard_off:
        score = max(score, float(cfg.off_score))
    off = hard_off or score >= float(cfg.off_score)
    elevated = (not off) and (score >= float(cfg.elevated_score) or bool(reasons))
    if off and not reasons:
        reasons.append(f"{variant}_score_off")
    elif elevated and not reasons:
        reasons.append(f"{variant}_score_elevated")

    return {
        "score": score,
        "elevated": elevated or off,
        "off": off,
        "hard_off": hard_off,
        "components": components,
        "medium_flags": medium_flags if variant == "R3" else {},
        "reason": "+".join(reasons) if reasons else None,
        "structure_break": structure_break,
        "failed_pack": failed_pack,
        "regime_15m": regime_15m,
    }


def score_short_risk(
    sig: BarSignals,
    cfg: RiskOffConfig,
    regime_15m: object | None = None,
) -> dict[str, Any]:
    """Mirror of :func:`score_long_risk` for short-side risk."""
    empty = {
        "score": 0.0,
        "elevated": False,
        "off": False,
        "hard_off": False,
        "components": {},
        "reason": "warmup_insufficient",
    }
    if not sig.warmup_ok:
        return empty

    variant = cfg.variant
    ctx = _regime_15m_bearish_context(regime_15m)
    components: dict[str, float] = {}
    hard_off = False
    reasons: list[str] = []

    structure_break = bool(
        sig.higher_low and (sig.break_above_lh or sig.break_above_swing_high)
    )
    weak_mom = bool(sig.close_gt_ema20 or sig.ema20_slope_pos or sig.ema9_gt_ema20)
    failed_pack = bool(
        (sig.failed_breakdown or sig.lower_wick_rejection)
        and sig.range_reentry_from_low
        and weak_mom
    )
    medium_flags = {
        "ema9_gt_ema20": sig.ema9_gt_ema20,
        "close_gt_ema20": sig.close_gt_ema20,
        "di_bullish": sig.di_bullish,
        "cum_up_2": sig.cum_up_2,
        "volume_bull_expand": sig.volume_bull_expand,
        "ema20_slope_pos": sig.ema20_slope_pos,
        "ema9_slope_pos": sig.ema9_slope_pos,
        "bullish_candle": sig.bullish_candle and sig.cum_up_2,
    }
    medium_count = sum(1 for v in medium_flags.values() if v)

    if variant == "R1":
        if structure_break and (sig.close_gt_ema20 or sig.ema20_slope_pos):
            hard_off = True
            components["structure_break"] = float(cfg.off_score)
            reasons.append("R1_structure_break_short")
        else:
            if sig.higher_low:
                components["higher_low"] = 1.0
            if sig.break_above_lh or sig.break_above_swing_high:
                components["break_above_lh_or_swing"] = 1.5
            if sig.close_gt_ema20 or sig.ema20_slope_pos:
                components["ema20_strong"] = 1.0
    elif variant == "R2":
        if failed_pack:
            hard_off = True
            components["failed_breakdown_pack"] = float(cfg.off_score)
            reasons.append("R2_failed_breakdown")
        else:
            if sig.failed_breakdown or sig.lower_wick_rejection:
                components["failed_or_rejection"] = 2.0
            if sig.range_reentry_from_low:
                components["range_reentry"] = 1.5
            if weak_mom:
                components["weak_momentum_vs_short"] = 1.5
            if sig.near_session_low or sig.near_prior_day_low:
                components["near_low"] = 0.5
    elif variant == "R3":
        if sig.atr_impulse_bull:
            hard_off = True
            components["atr_impulse"] = float(cfg.off_score)
            reasons.append("R3_atr_impulse_bull")
        elif sig.cum_up_3 and sig.volume_bull_expand:
            hard_off = True
            components["cum3_bull_vol"] = float(cfg.off_score)
            reasons.append("R3_cum_up_bull_vol")
        elif medium_count >= 3:
            hard_off = True
            components["medium_flags"] = float(cfg.off_score)
            reasons.append("R3_medium_flags_short")
        else:
            if sig.cum_up_2:
                components["cum_up_2"] = 1.0
            if sig.cum_up_3:
                components["cum_up_3"] = 1.5
            if sig.cum_up_4:
                components["cum_up_4"] = 2.0
            if sig.volume_bull_expand:
                components["volume_bull"] = 1.0
            components["medium_count"] = float(medium_count)
    elif variant == "R4":
        if structure_break:
            components["structure"] = 2.5
        if failed_pack or sig.failed_breakdown:
            components["failed"] = 2.0
        if sig.lower_wick_rejection:
            components["wick_reject"] = 1.0
        if sig.atr_impulse_bull:
            components["impulse"] = 2.0
        if sig.cum_up_2:
            components["cum2"] = 0.75
        if sig.cum_up_3:
            components["cum3"] = 1.0
        if sig.cum_up_4:
            components["cum4"] = 1.25
        if sig.ema9_gt_ema20:
            components["ema_cross"] = 0.75
        if sig.close_gt_ema20:
            components["close_gt_ema20"] = 0.75
        if sig.ema20_slope_pos:
            components["ema20_slope"] = 0.5
        if sig.di_bullish:
            components["di_bullish"] = 0.75
        if sig.volume_bull_expand:
            components["vol_bull"] = 0.75
        if ctx["regime_bullish"] or ctx["regime_weakness"] or ctx["regime_transition"]:
            components["ctx_15m"] = 1.0
        if ctx["regime_strong_bullish"]:
            components["ctx_15m_strong"] = 1.5
        if structure_break and sig.atr_impulse_bull:
            hard_off = True
            reasons.append("R4_structure_plus_impulse_short")
    else:
        raise ValueError(f"unknown variant {variant}")

    score = float(sum(components.values()))
    if hard_off:
        score = max(score, float(cfg.off_score))
    off = hard_off or score >= float(cfg.off_score)
    elevated = (not off) and (score >= float(cfg.elevated_score) or bool(reasons))
    if off and not reasons:
        reasons.append(f"{variant}_score_off_short")
    elif elevated and not reasons:
        reasons.append(f"{variant}_score_elevated_short")

    return {
        "score": score,
        "elevated": elevated or off,
        "off": off,
        "hard_off": hard_off,
        "components": components,
        "medium_flags": medium_flags if variant == "R3" else {},
        "reason": "+".join(reasons) if reasons else None,
        "structure_break": structure_break,
        "failed_pack": failed_pack,
        "regime_15m": regime_15m,
    }


@dataclass
class RiskRuntime:
    """Mutable Risk-Off FSM runtime (no outcomes)."""

    state: RiskState = "unavailable"
    age_bars: int = 0
    entered_at: str | None = None
    entry_reason: str | None = None
    exit_reason: str | None = None
    consec_close_above_ema20: int = 0
    consec_close_below_ema20: int = 0
    consec_score_below_exit_long: int = 0
    consec_score_below_exit_short: int = 0
    last_lower_high_price: float | None = None
    last_higher_low_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _update_consec_closes(rt: RiskRuntime, sig: BarSignals) -> None:
    if sig.close_gt_ema20:
        rt.consec_close_above_ema20 += 1
        rt.consec_close_below_ema20 = 0
    elif sig.close_lt_ema20:
        rt.consec_close_below_ema20 += 1
        rt.consec_close_above_ema20 = 0
    else:
        rt.consec_close_above_ema20 = 0
        rt.consec_close_below_ema20 = 0


def _long_structure_recovery(sig: BarSignals, rt: RiskRuntime) -> bool:
    """HL present and close breaks above last LH (or last swing high)."""
    if not sig.higher_low:
        return False
    level = rt.last_lower_high_price
    if level is None:
        level = sig.last_lower_high_price or sig.last_swing_high
    if level is None or sig.close is None:
        return False
    return sig.close > float(level)


def _short_structure_recovery(sig: BarSignals, rt: RiskRuntime) -> bool:
    if not sig.lower_high:
        return False
    level = rt.last_higher_low_price
    if level is None:
        level = sig.last_higher_low_price or sig.last_swing_low
    if level is None or sig.close is None:
        return False
    return sig.close < float(level)


def _enter(
    rt: RiskRuntime,
    state: RiskState,
    *,
    when: str,
    reason: str,
    meta: dict[str, Any],
) -> None:
    rt.state = state
    rt.entered_at = when
    rt.age_bars = 1
    rt.entry_reason = reason
    rt.exit_reason = None
    meta["entry_reason"] = reason
    meta["transition"] = f"{meta['state_before']}->{state}"


def step_risk_state(
    rt: RiskRuntime,
    sig: BarSignals,
    long_score: Mapping[str, Any],
    short_score: Mapping[str, Any],
    cfg: RiskOffConfig,
    b3_state: object | None = None,
) -> tuple[RiskRuntime, dict[str, Any]]:
    """Advance Risk-Off FSM one closed 5m bar."""
    meta: dict[str, Any] = {
        "state_before": rt.state,
        "entry_reason": None,
        "exit_reason": None,
        "transition": None,
        "b3_state": b3_state,
    }
    b3 = str(b3_state or "").strip().lower()

    if not sig.warmup_ok:
        rt.state = "unavailable"
        rt.age_bars = 0
        rt.entered_at = None
        meta["transition"] = "unavailable"
        return rt, meta

    if sig.lower_high and sig.last_swing_high is not None:
        rt.last_lower_high_price = sig.last_swing_high
    if sig.higher_low and sig.last_swing_low is not None:
        rt.last_higher_low_price = sig.last_swing_low

    _update_consec_closes(rt, sig)

    long_off = bool(long_score.get("off") or long_score.get("hard_off"))
    long_elev = bool(long_score.get("elevated")) and not long_off
    short_off = bool(short_score.get("off") or short_score.get("hard_off"))
    short_elev = bool(short_score.get("elevated")) and not short_off
    long_sc = float(long_score.get("score") or 0.0)
    short_sc = float(short_score.get("score") or 0.0)

    if long_sc < float(cfg.exit_score):
        rt.consec_score_below_exit_long += 1
    else:
        rt.consec_score_below_exit_long = 0
    if short_sc < float(cfg.exit_score):
        rt.consec_score_below_exit_short += 1
    else:
        rt.consec_score_below_exit_short = 0

    prev = rt.state

    # Covered transitions: B3 strong trend supersedes active risk-off on that side.
    if b3 == "strong_bearish" and prev in {
        "long_risk_off",
        "long_risk_elevated",
        "covered_by_strong_bearish",
    }:
        if prev != "covered_by_strong_bearish":
            _enter(
                rt,
                "covered_by_strong_bearish",
                when=sig.decision_time,
                reason="b3_strong_bearish_covers_long_risk",
                meta=meta,
            )
            return rt, meta
        rt.age_bars += 1
        meta["transition"] = None
        return rt, meta

    if b3 == "strong_bullish" and prev in {
        "short_risk_off",
        "short_risk_elevated",
        "covered_by_strong_bullish",
    }:
        if prev != "covered_by_strong_bullish":
            _enter(
                rt,
                "covered_by_strong_bullish",
                when=sig.decision_time,
                reason="b3_strong_bullish_covers_short_risk",
                meta=meta,
            )
            return rt, meta
        rt.age_bars += 1
        meta["transition"] = None
        return rt, meta

    # Leave covered when B3 releases.
    if prev == "covered_by_strong_bearish" and b3 != "strong_bearish":
        if long_off:
            _enter(
                rt,
                "long_risk_off",
                when=sig.decision_time,
                reason="leave_covered_still_long_off",
                meta=meta,
            )
            return rt, meta
        rt.state = "normal"
        rt.age_bars = 0
        rt.exit_reason = "b3_released"
        meta["exit_reason"] = rt.exit_reason
        meta["transition"] = "covered_by_strong_bearish->normal"
        prev = "normal"
    if prev == "covered_by_strong_bullish" and b3 != "strong_bullish":
        if short_off:
            _enter(
                rt,
                "short_risk_off",
                when=sig.decision_time,
                reason="leave_covered_still_short_off",
                meta=meta,
            )
            return rt, meta
        rt.state = "normal"
        rt.age_bars = 0
        rt.exit_reason = "b3_released"
        meta["exit_reason"] = rt.exit_reason
        meta["transition"] = "covered_by_strong_bullish->normal"
        prev = "normal"

    # Fresh entry from normal / unavailable.
    if prev in {"normal", "unavailable"}:
        # Prefer long-off when both fire (March focus); still record short separately.
        if long_off:
            if b3 == "strong_bearish":
                _enter(
                    rt,
                    "covered_by_strong_bearish",
                    when=sig.decision_time,
                    reason=str(long_score.get("reason") or "long_off_covered"),
                    meta=meta,
                )
            else:
                _enter(
                    rt,
                    "long_risk_off",
                    when=sig.decision_time,
                    reason=str(long_score.get("reason") or "long_off"),
                    meta=meta,
                )
            return rt, meta
        if short_off:
            if b3 == "strong_bullish":
                _enter(
                    rt,
                    "covered_by_strong_bullish",
                    when=sig.decision_time,
                    reason=str(short_score.get("reason") or "short_off_covered"),
                    meta=meta,
                )
            else:
                _enter(
                    rt,
                    "short_risk_off",
                    when=sig.decision_time,
                    reason=str(short_score.get("reason") or "short_off"),
                    meta=meta,
                )
            return rt, meta
        if long_elev:
            _enter(
                rt,
                "long_risk_elevated",
                when=sig.decision_time,
                reason=str(long_score.get("reason") or "long_elevated"),
                meta=meta,
            )
            return rt, meta
        if short_elev:
            _enter(
                rt,
                "short_risk_elevated",
                when=sig.decision_time,
                reason=str(short_score.get("reason") or "short_elevated"),
                meta=meta,
            )
            return rt, meta
        rt.state = "normal"
        rt.age_bars = rt.age_bars + 1 if prev == "normal" else 0
        if prev == "unavailable":
            rt.entered_at = sig.decision_time
        meta["transition"] = f"{prev}->normal" if prev != "normal" else None
        return rt, meta

    # --- long elevated ---
    if prev == "long_risk_elevated":
        rt.age_bars += 1
        if long_off:
            if b3 == "strong_bearish":
                _enter(
                    rt,
                    "covered_by_strong_bearish",
                    when=sig.decision_time,
                    reason="upgrade_elevated_to_covered",
                    meta=meta,
                )
            else:
                _enter(
                    rt,
                    "long_risk_off",
                    when=sig.decision_time,
                    reason="upgrade_elevated_to_off",
                    meta=meta,
                )
            return rt, meta
        can_exit = rt.age_bars >= cfg.min_hold_bars
        exit_reasons: list[str] = []
        if can_exit and rt.consec_close_above_ema20 >= cfg.exit_below_bars:
            exit_reasons.append("two_closes_above_ema20")
        if can_exit and rt.consec_score_below_exit_long >= cfg.exit_below_bars:
            exit_reasons.append("score_below_exit")
        if can_exit and _long_structure_recovery(sig, rt):
            exit_reasons.append("structure_recovery")
        if can_exit and b3 == "strong_bullish":
            exit_reasons.append("b3_strong_bullish")
        if exit_reasons:
            rt.state = "normal"
            rt.exit_reason = "+".join(exit_reasons)
            rt.age_bars = 0
            rt.entered_at = sig.decision_time
            meta["exit_reason"] = rt.exit_reason
            meta["transition"] = "long_risk_elevated->normal"
            return rt, meta
        return rt, meta

    # --- long off ---
    if prev == "long_risk_off":
        rt.age_bars += 1
        if b3 == "strong_bearish":
            _enter(
                rt,
                "covered_by_strong_bearish",
                when=sig.decision_time,
                reason="b3_covers_long_off",
                meta=meta,
            )
            return rt, meta
        can_exit = rt.age_bars >= cfg.min_hold_bars
        exit_reasons = []
        if can_exit and rt.consec_close_above_ema20 >= cfg.exit_below_bars:
            exit_reasons.append("two_closes_above_ema20")
        if can_exit and rt.consec_score_below_exit_long >= cfg.exit_below_bars:
            exit_reasons.append("score_below_exit")
        if can_exit and _long_structure_recovery(sig, rt):
            exit_reasons.append("structure_recovery")
        if can_exit and b3 == "strong_bullish":
            exit_reasons.append("b3_strong_bullish")
        # Single green candle must not clear risk-off (require consec / score / structure).
        if exit_reasons:
            rt.state = "normal"
            rt.exit_reason = "+".join(exit_reasons)
            rt.age_bars = 0
            rt.entered_at = sig.decision_time
            meta["exit_reason"] = rt.exit_reason
            meta["transition"] = "long_risk_off->normal"
            return rt, meta
        return rt, meta

    # --- short elevated (mirror) ---
    if prev == "short_risk_elevated":
        rt.age_bars += 1
        if short_off:
            if b3 == "strong_bullish":
                _enter(
                    rt,
                    "covered_by_strong_bullish",
                    when=sig.decision_time,
                    reason="upgrade_elevated_to_covered_short",
                    meta=meta,
                )
            else:
                _enter(
                    rt,
                    "short_risk_off",
                    when=sig.decision_time,
                    reason="upgrade_elevated_to_off_short",
                    meta=meta,
                )
            return rt, meta
        can_exit = rt.age_bars >= cfg.min_hold_bars
        exit_reasons = []
        if can_exit and rt.consec_close_below_ema20 >= cfg.exit_below_bars:
            exit_reasons.append("two_closes_below_ema20")
        if can_exit and rt.consec_score_below_exit_short >= cfg.exit_below_bars:
            exit_reasons.append("score_below_exit")
        if can_exit and _short_structure_recovery(sig, rt):
            exit_reasons.append("structure_recovery")
        if can_exit and b3 == "strong_bearish":
            exit_reasons.append("b3_strong_bearish")
        if exit_reasons:
            rt.state = "normal"
            rt.exit_reason = "+".join(exit_reasons)
            rt.age_bars = 0
            rt.entered_at = sig.decision_time
            meta["exit_reason"] = rt.exit_reason
            meta["transition"] = "short_risk_elevated->normal"
            return rt, meta
        return rt, meta

    # --- short off ---
    if prev == "short_risk_off":
        rt.age_bars += 1
        if b3 == "strong_bullish":
            _enter(
                rt,
                "covered_by_strong_bullish",
                when=sig.decision_time,
                reason="b3_covers_short_off",
                meta=meta,
            )
            return rt, meta
        can_exit = rt.age_bars >= cfg.min_hold_bars
        exit_reasons = []
        if can_exit and rt.consec_close_below_ema20 >= cfg.exit_below_bars:
            exit_reasons.append("two_closes_below_ema20")
        if can_exit and rt.consec_score_below_exit_short >= cfg.exit_below_bars:
            exit_reasons.append("score_below_exit")
        if can_exit and _short_structure_recovery(sig, rt):
            exit_reasons.append("structure_recovery")
        if can_exit and b3 == "strong_bearish":
            exit_reasons.append("b3_strong_bearish")
        if exit_reasons:
            rt.state = "normal"
            rt.exit_reason = "+".join(exit_reasons)
            rt.age_bars = 0
            rt.entered_at = sig.decision_time
            meta["exit_reason"] = rt.exit_reason
            meta["transition"] = "short_risk_off->normal"
            return rt, meta
        return rt, meta

    return rt, meta


def would_block_long(risk_state: object, *, b3_state: object | None = None) -> bool:
    """Hard long block: risk-off / covered / B3 strong_bearish."""
    s = str(risk_state or "")
    b3 = str(b3_state or "").strip().lower()
    if b3 == "strong_bearish":
        return True
    return s in {"long_risk_off", "covered_by_strong_bearish"}


def would_block_short(risk_state: object, *, b3_state: object | None = None) -> bool:
    s = str(risk_state or "")
    b3 = str(b3_state or "").strip().lower()
    if b3 == "strong_bullish":
        return True
    return s in {"short_risk_off", "covered_by_strong_bullish"}


def blocking_layer(
    risk_state: object,
    b3_state: object | None,
    side: str,
) -> BlockingLayer:
    """Which layer would block ``side`` (B3 has priority over Risk-Off)."""
    side_l = str(side).lower()
    b3 = str(b3_state or "").strip().lower()
    rs = str(risk_state or "")

    if side_l == "long":
        if b3 == "strong_bearish":
            return "b3"
        if rs == "covered_by_strong_bearish":
            return "covered"
        if rs == "long_risk_off":
            return "risk_off"
        if rs == "long_risk_elevated":
            return "risk_elevated"
        return "none"
    if side_l == "short":
        if b3 == "strong_bullish":
            return "b3"
        if rs == "covered_by_strong_bullish":
            return "covered"
        if rs == "short_risk_off":
            return "risk_off"
        if rs == "short_risk_elevated":
            return "risk_elevated"
        return "none"
    return "none"


def momentum_candle_quality(
    sig: BarSignals,
    side: str = "long",
) -> dict[str, Any]:
    """Descriptive confirmation-candle quality: rising / stable / falling."""
    side_l = str(side).lower()
    body_ok = False
    close_loc = None
    atr_ratio = None
    if sig.open is not None and sig.high is not None and sig.low is not None and sig.close is not None:
        rng = sig.high - sig.low
        body = abs(sig.close - sig.open)
        body_ok = bool(rng > 0 and (body / rng) >= 0.5)
        if rng > 0:
            if side_l == "long":
                close_loc = (sig.close - sig.low) / rng
            else:
                close_loc = (sig.high - sig.close) / rng
        if sig.atr is not None and sig.atr > 0:
            atr_ratio = rng / sig.atr

    directional = (sig.bullish_candle if side_l == "long" else sig.bearish_candle)
    adverse = (sig.bearish_candle if side_l == "long" else sig.bullish_candle)
    impulse_adverse = (
        sig.atr_impulse_bear if side_l == "long" else sig.atr_impulse_bull
    )
    ema_ok = (
        (sig.close_gt_ema20 or sig.ema9_gt_ema20)
        if side_l == "long"
        else (sig.close_lt_ema20 or sig.ema9_lt_ema20)
    )

    score = 0
    if directional and body_ok:
        score += 2
    elif directional:
        score += 1
    if close_loc is not None and close_loc >= 0.6:
        score += 1
    if atr_ratio is not None and 0.3 <= atr_ratio <= 3.0:
        score += 1
    if ema_ok:
        score += 1
    if adverse:
        score -= 2
    if impulse_adverse:
        score -= 2

    if score >= 3:
        label: Literal["rising", "stable", "falling"] = "rising"
    elif score <= 0:
        label = "falling"
    else:
        label = "stable"

    return {
        "side": side_l,
        "quality_label": label,
        "quality_score": score,
        "body_ok": body_ok,
        "close_location_ratio": close_loc,
        "range_atr_ratio": atr_ratio,
        "directional": directional,
        "adverse": adverse,
    }


def _series_lookup_frame(
    mapping: object | None,
    *,
    value_col: str,
) -> pd.DataFrame:
    """Normalize decision_time -> value mapping for merge_asof."""
    if mapping is None:
        return pd.DataFrame(columns=["decision_time", value_col])
    if isinstance(mapping, pd.DataFrame):
        df = mapping.copy()
        if "decision_time" not in df.columns:
            raise ValueError(f"{value_col} frame needs decision_time column")
        cols = [c for c in df.columns if c != "decision_time"]
        if value_col not in df.columns:
            if len(cols) == 1:
                df = df.rename(columns={cols[0]: value_col})
            else:
                raise ValueError(f"cannot resolve {value_col} column")
        out = df[["decision_time", value_col]].copy()
    elif isinstance(mapping, pd.Series):
        out = mapping.rename(value_col).reset_index()
        if out.columns[0] != "decision_time":
            out = out.rename(columns={out.columns[0]: "decision_time"})
    elif isinstance(mapping, Mapping):
        out = pd.DataFrame(
            {
                "decision_time": list(mapping.keys()),
                value_col: list(mapping.values()),
            }
        )
    else:
        raise TypeError(f"unsupported mapping type for {value_col}: {type(mapping)}")
    out["decision_time"] = pd.to_datetime(out["decision_time"], utc=True)
    return out.sort_values("decision_time").drop_duplicates("decision_time", keep="last")


def _rolling_vol_median(volumes: pd.Series, window: int, i: int) -> float | None:
    if i <= 0 or window <= 0:
        return None
    start = max(0, i - int(window))
    # Causal median of prior bars only (exclude current).
    window_vals = [_finite(v) for v in volumes.iloc[start:i].tolist()]
    vals = [v for v in window_vals if v is not None]
    if not vals:
        return None
    return float(np.median(np.asarray(vals, dtype="float64")))


def _track_break_levels(
    sig: BarSignals,
    *,
    breakout_level: float | None,
    breakdown_level: float | None,
) -> tuple[float | None, float | None]:
    """Update recent session breakout / breakdown levels after signal compute."""
    bo = breakout_level
    bd = breakdown_level
    if (
        sig.session_high is not None
        and sig.high is not None
        and sig.close is not None
        and sig.high >= float(sig.session_high)
        and sig.close >= float(sig.session_high)
    ):
        bo = float(sig.session_high)
    if (
        sig.prior_day_high is not None
        and sig.close is not None
        and sig.high is not None
        and sig.high >= float(sig.prior_day_high)
        and sig.close >= float(sig.prior_day_high)
    ):
        bo = float(sig.prior_day_high)
    if (
        sig.session_low is not None
        and sig.low is not None
        and sig.close is not None
        and sig.low <= float(sig.session_low)
        and sig.close <= float(sig.session_low)
    ):
        bd = float(sig.session_low)
    if (
        sig.prior_day_low is not None
        and sig.close is not None
        and sig.low is not None
        and sig.low <= float(sig.prior_day_low)
        and sig.close <= float(sig.prior_day_low)
    ):
        bd = float(sig.prior_day_low)
    # Clear breakout level once failed (close back inside).
    if bo is not None and sig.close is not None and sig.close < float(bo):
        # Keep level for this bar's failed detection; clear going forward.
        pass
    return bo, bd


def run_risk_off_timeline(
    candles_5m: pd.DataFrame,
    cfg: RiskOffConfig,
    scanner_cfg: RegimeScannerConfig | None = None,
    regime_15m_by_decision: object | None = None,
    b3_by_decision: object | None = None,
    start: object | None = None,
    end: object | None = None,
) -> pd.DataFrame:
    """Walk closed 5m bars; warm before ``start``; emit rows in ``[start, end)``."""
    if candles_5m.empty:
        return pd.DataFrame()

    scfg = scanner_cfg or default_regime_scanner_config()
    tf_cfg = scfg.with_timeframe("5m") if hasattr(scfg, "with_timeframe") else scfg
    frame = compute_indicator_frame(candles_5m, config=tf_cfg)
    if frame.empty:
        return pd.DataFrame()
    pivots = find_confirmed_pivots(frame, config=tf_cfg)

    start_ts = _to_utc(start) if start is not None else None
    end_ts = _to_utc(end) if end is not None else None

    regime_df = _series_lookup_frame(regime_15m_by_decision, value_col="regime_15m")
    b3_df = _series_lookup_frame(b3_by_decision, value_col="b3_state")

    # Pre-build decision_time index for asof lookup of context.
    dec_times = [
        _to_utc(frame.iloc[i]["timestamp"]) + pd.Timedelta(minutes=5)
        for i in range(len(frame))
    ]
    dec_frame = pd.DataFrame({"decision_time": dec_times, "_i": list(range(len(frame)))})
    if not regime_df.empty:
        dec_frame = pd.merge_asof(
            dec_frame.sort_values("decision_time"),
            regime_df,
            on="decision_time",
            direction="backward",
        )
    else:
        dec_frame["regime_15m"] = None
    if not b3_df.empty:
        dec_frame = pd.merge_asof(
            dec_frame.sort_values("decision_time"),
            b3_df,
            on="decision_time",
            direction="backward",
        )
    else:
        dec_frame["b3_state"] = None
    ctx_by_i = {
        int(r["_i"]): (r.get("regime_15m"), r.get("b3_state"))
        for _, r in dec_frame.iterrows()
    }

    has_volume = "volume" in frame.columns
    volumes = frame["volume"] if has_volume else pd.Series([np.nan] * len(frame))

    rt = RiskRuntime()
    breakout_level: float | None = None
    breakdown_level: float | None = None
    rows: list[dict[str, Any]] = []
    prev_day: pd.Timestamp | None = None

    for i in range(len(frame)):
        ts = _to_utc(frame.iloc[i]["timestamp"])
        decision = ts + pd.Timedelta(minutes=5)
        day = ts.normalize()
        if prev_day is not None and day != prev_day:
            # New UTC session: reset intra-session breakout tags.
            breakout_level = None
            breakdown_level = None
        prev_day = day

        if end_ts is not None and decision >= end_ts:
            break

        vol_med = _rolling_vol_median(volumes, cfg.vol_median_window, i) if has_volume else None
        sig = compute_bar_signals(
            frame,
            i,
            pivots,
            cfg,
            tf_cfg,
            vol_med,
            breakout_level,
            recent_breakdown_level=breakdown_level,
        )
        regime_15m, b3_state = ctx_by_i.get(i, (None, None))
        long_score = score_long_risk(sig, cfg, regime_15m=regime_15m)
        short_score = score_short_risk(sig, cfg, regime_15m=regime_15m)
        rt, meta = step_risk_state(
            rt, sig, long_score, short_score, cfg, b3_state=b3_state
        )

        # Update breakout tracking after scoring this bar.
        new_bo, new_bd = _track_break_levels(
            sig, breakout_level=breakout_level, breakdown_level=breakdown_level
        )
        # If failed this bar, clear level after consuming it.
        if sig.failed_breakout:
            breakout_level = None
        else:
            breakout_level = new_bo
        if sig.failed_breakdown:
            breakdown_level = None
        else:
            breakdown_level = new_bd

        if start_ts is not None and decision < start_ts:
            continue

        mq_long = momentum_candle_quality(sig, side="long")
        mq_short = momentum_candle_quality(sig, side="short")
        risk_state = rt.state
        row = {
            **sig.to_dict(),
            "risk_variant": cfg.variant,
            "risk_enabled_flag": cfg.enabled,
            "regime_15m": regime_15m,
            "b3_state": b3_state,
            "long_risk_score": long_score.get("score"),
            "long_risk_elevated": long_score.get("elevated"),
            "long_risk_off_flag": long_score.get("off"),
            "long_hard_off": long_score.get("hard_off"),
            "long_risk_reason": long_score.get("reason"),
            "long_components_json": str(long_score.get("components")),
            "short_risk_score": short_score.get("score"),
            "short_risk_elevated": short_score.get("elevated"),
            "short_risk_off_flag": short_score.get("off"),
            "short_hard_off": short_score.get("hard_off"),
            "short_risk_reason": short_score.get("reason"),
            "short_components_json": str(short_score.get("components")),
            "risk_state": risk_state,
            "state_age_bars": rt.age_bars,
            "state_entered_at": rt.entered_at,
            "entry_reason": meta.get("entry_reason") or rt.entry_reason,
            "exit_reason": meta.get("exit_reason"),
            "transition": meta.get("transition"),
            "would_block_long": would_block_long(risk_state, b3_state=b3_state),
            "would_block_short": would_block_short(risk_state, b3_state=b3_state),
            "blocking_layer_long": blocking_layer(risk_state, b3_state, "long"),
            "blocking_layer_short": blocking_layer(risk_state, b3_state, "short"),
            "momentum_quality_long": mq_long.get("quality_label"),
            "momentum_quality_short": mq_short.get("quality_label"),
            "confirm_candles_normal": cfg.confirm_candles_normal,
            "confirm_candles_elevated": cfg.confirm_candles_elevated,
        }
        rows.append(row)

    return pd.DataFrame(rows)


def assert_outcomes_do_not_affect_risk(
    risk_rows: pd.DataFrame,
    outcomes: pd.DataFrame | None,
) -> None:
    """Safety helper: outcome columns must not appear as risk computation inputs."""
    if outcomes is None:
        return
    forbidden = {"mfe", "mae", "tp_hit", "deepest_drop", "pnl", "returned_to_signal"}
    overlap = forbidden.intersection({c.lower() for c in risk_rows.columns})
    assert not overlap, f"risk frame unexpectedly contains outcome columns: {overlap}"
