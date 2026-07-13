"""Research-only 15m Strong-Trend Direction Gate (counterfactual).

Does NOT replace regime classification or mutate setup/HTF policy.
Gate is disabled by default (``DirectionGateConfig.enabled=False``).

State updates only on fully closed 15m bars (close_time <= decision_time).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

import pandas as pd

from research.regime_scanner.config import RegimeScannerConfig, default_regime_scanner_config
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.structure import classify_swing_structure
from research.regime_scanner.swings import ConfirmedPivot, find_confirmed_pivots
from research.regime_scanner.timeframes import aggregate_candles

GateState = Literal["strong_bearish", "strong_bullish", "neutral", "unavailable"]
GateVariant = Literal["B1", "B2", "B3"]

WARMUP_NOTE = (
    "15m warmup uses RegimeScannerConfig.min_warmup_candles after with_timeframe('15m') "
    "(~346×15m bars ≈ max EMA200 + max slope window + pivot_right). "
    "Before warmup, state is 'unavailable'. EMA200 NaN does not crash; "
    "missing EMA200 is ignored for entry (not required for B1–B3)."
)


@dataclass(frozen=True)
class DirectionGateConfig:
    """Configurable Strong-Trend Direction Gate (research)."""

    enabled: bool = False  # default OFF — never auto-wire into live/pipeline
    variant: GateVariant = "B1"
    adx_min: float = 18.0
    atr_pct_min: float = 0.12  # chop filter; used as optional confirmation
    require_atr_pct: bool = False
    ema20_slope_key: str = "ema_20_slope_12_pct"
    ema59_slope_key: str = "ema_59_slope_48_pct"
    min_hold_bars: int = 2
    # Exit: consecutive closed 15m bars
    exit_closes_beyond_ema20: int = 2
    exit_ema9_cross_bars: int = 2
    # Direct flip requires bullish/bearish entry with this many *extra* confirms
    flip_extra_confirmations: int = 1
    structure_epsilon_pct: float = 0.01
    use_prior_day_low_break: bool = True  # research signal only (extra confirm)
    use_prior_day_high_break: bool = True


@dataclass
class GateRuntimeState:
    state: GateState = "unavailable"
    entered_at: str | None = None
    age_bars: int = 0
    entry_reason: str | None = None
    exit_reason: str | None = None
    consec_close_above_ema20: int = 0
    consec_close_below_ema20: int = 0
    consec_ema9_ge_ema20: int = 0
    consec_ema9_le_ema20: int = 0
    last_15m_open: str | None = None
    last_lower_high_price: float | None = None
    last_higher_low_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_direction_gate_config(*, variant: GateVariant = "B1") -> DirectionGateConfig:
    return DirectionGateConfig(variant=variant, enabled=False)


def _finite(v: object) -> float | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def _bool(v: object) -> bool:
    return bool(v)


@dataclass
class BarFeatures:
    """Transparent per-bar criteria for audit visibility."""

    bar_open: str
    bar_close_time: str
    close: float | None
    ema_9: float | None
    ema_20: float | None
    ema_59: float | None
    ema_200: float | None
    ema20_slope: float | None
    ema59_slope: float | None
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    atr_pct: float | None
    last_swing_high: float | None
    last_swing_low: float | None
    last_swing_high_ts: str | None
    last_swing_low_ts: str | None
    structure_high: str | None
    structure_low: str | None
    lower_high: bool = False
    lower_low: bool = False
    higher_high: bool = False
    higher_low: bool = False
    break_below_swing_low: bool = False
    break_above_swing_high: bool = False
    break_prior_day_low: bool = False
    break_prior_day_high: bool = False
    prior_day_low: float | None = None
    prior_day_high: float | None = None
    warmup_ok: bool = False
    # trend bits
    close_lt_ema20: bool = False
    close_gt_ema20: bool = False
    ema9_lt_ema20: bool = False
    ema9_gt_ema20: bool = False
    ema20_lt_ema59: bool = False
    ema20_gt_ema59: bool = False
    ema9_lt_ema20_lt_ema59: bool = False
    ema9_gt_ema20_gt_ema59: bool = False
    ema20_slope_neg: bool = False
    ema20_slope_pos: bool = False
    ema59_slope_neg: bool = False
    ema59_slope_pos: bool = False
    di_bearish: bool = False
    di_bullish: bool = False
    adx_ok: bool = False
    atr_ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pivots_as_of(pivots: list[ConfirmedPivot], bar_index: int) -> list[ConfirmedPivot]:
    return [p for p in pivots if int(p.confirmation_index) <= int(bar_index)]


def _latest_same_side(pivots: list[ConfirmedPivot], side: str, count: int = 2) -> list[ConfirmedPivot]:
    matched = [p for p in pivots if p.pivot_type == side]
    return matched[-count:] if matched else []


def compute_bar_features(
    frame: pd.DataFrame,
    bar_index: int,
    pivots: list[ConfirmedPivot],
    *,
    cfg: DirectionGateConfig,
    scanner_cfg: RegimeScannerConfig,
    prior_day_low: float | None,
    prior_day_high: float | None,
) -> BarFeatures:
    row = frame.iloc[bar_index]
    ts = pd.Timestamp(row["timestamp"])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    close_time = ts + pd.Timedelta(minutes=15)

    close = _finite(row.get("close"))
    ema9 = _finite(row.get("ema_9"))
    ema20 = _finite(row.get("ema_20"))
    ema59 = _finite(row.get("ema_59"))
    ema200 = _finite(row.get("ema_200"))
    slope20 = _finite(row.get(cfg.ema20_slope_key))
    slope59 = _finite(row.get(cfg.ema59_slope_key))
    adx = _finite(row.get("adx"))
    plus_di = _finite(row.get("plus_di"))
    minus_di = _finite(row.get("minus_di"))
    atr_pct = _finite(row.get("atr_pct"))

    warmup_ok = bar_index + 1 >= int(scanner_cfg.min_warmup_candles)

    asof = _pivots_as_of(pivots, bar_index)
    highs = _latest_same_side(asof, "high", 2)
    lows = _latest_same_side(asof, "low", 2)
    last_sh = highs[-1] if highs else None
    last_sl = lows[-1] if lows else None

    structure_high = None
    structure_low = None
    lower_high = lower_low = higher_high = higher_low = False
    if len(highs) == 2:
        st = classify_swing_structure(
            highs[0].price, highs[1].price, side="high", epsilon_pct=cfg.structure_epsilon_pct
        )
        structure_high = str(st["structure_type"])
        lower_high = structure_high == "lower_high"
        higher_high = structure_high == "higher_high"
    if len(lows) == 2:
        st = classify_swing_structure(
            lows[0].price, lows[1].price, side="low", epsilon_pct=cfg.structure_epsilon_pct
        )
        structure_low = str(st["structure_type"])
        lower_low = structure_low == "lower_low"
        higher_low = structure_low == "higher_low"

    break_below = bool(
        close is not None and last_sl is not None and close < float(last_sl.price)
    )
    break_above = bool(
        close is not None and last_sh is not None and close > float(last_sh.price)
    )
    break_pdl = bool(
        cfg.use_prior_day_low_break
        and close is not None
        and prior_day_low is not None
        and close < float(prior_day_low)
    )
    break_pdh = bool(
        cfg.use_prior_day_high_break
        and close is not None
        and prior_day_high is not None
        and close > float(prior_day_high)
    )

    feat = BarFeatures(
        bar_open=ts.isoformat(),
        bar_close_time=close_time.isoformat(),
        close=close,
        ema_9=ema9,
        ema_20=ema20,
        ema_59=ema59,
        ema_200=ema200,
        ema20_slope=slope20,
        ema59_slope=slope59,
        adx=adx,
        plus_di=plus_di,
        minus_di=minus_di,
        atr_pct=atr_pct,
        last_swing_high=float(last_sh.price) if last_sh else None,
        last_swing_low=float(last_sl.price) if last_sl else None,
        last_swing_high_ts=last_sh.confirmation_timestamp if last_sh else None,
        last_swing_low_ts=last_sl.confirmation_timestamp if last_sl else None,
        structure_high=structure_high,
        structure_low=structure_low,
        lower_high=lower_high,
        lower_low=lower_low,
        higher_high=higher_high,
        higher_low=higher_low,
        break_below_swing_low=break_below,
        break_above_swing_high=break_above,
        break_prior_day_low=break_pdl,
        break_prior_day_high=break_pdh,
        prior_day_low=prior_day_low,
        prior_day_high=prior_day_high,
        warmup_ok=warmup_ok,
        close_lt_ema20=bool(close is not None and ema20 is not None and close < ema20),
        close_gt_ema20=bool(close is not None and ema20 is not None and close > ema20),
        ema9_lt_ema20=bool(ema9 is not None and ema20 is not None and ema9 < ema20),
        ema9_gt_ema20=bool(ema9 is not None and ema20 is not None and ema9 > ema20),
        ema20_lt_ema59=bool(ema20 is not None and ema59 is not None and ema20 < ema59),
        ema20_gt_ema59=bool(ema20 is not None and ema59 is not None and ema20 > ema59),
        ema9_lt_ema20_lt_ema59=bool(
            ema9 is not None
            and ema20 is not None
            and ema59 is not None
            and ema9 < ema20 < ema59
        ),
        ema9_gt_ema20_gt_ema59=bool(
            ema9 is not None
            and ema20 is not None
            and ema59 is not None
            and ema9 > ema20 > ema59
        ),
        ema20_slope_neg=bool(slope20 is not None and slope20 < 0),
        ema20_slope_pos=bool(slope20 is not None and slope20 > 0),
        ema59_slope_neg=bool(slope59 is not None and slope59 < 0),
        ema59_slope_pos=bool(slope59 is not None and slope59 > 0),
        di_bearish=bool(minus_di is not None and plus_di is not None and minus_di > plus_di),
        di_bullish=bool(plus_di is not None and minus_di is not None and plus_di > minus_di),
        adx_ok=bool(adx is not None and adx >= cfg.adx_min),
        atr_ok=bool(atr_pct is not None and atr_pct >= cfg.atr_pct_min),
    )
    return feat


def _bearish_confirm_flags(f: BarFeatures, cfg: DirectionGateConfig) -> dict[str, bool]:
    flags = {
        "ema20_lt_ema59": f.ema20_lt_ema59,
        "ema59_slope_neg": f.ema59_slope_neg,
        "di_bearish": f.di_bearish,
        "adx_ok": f.adx_ok,
        "lower_high": f.lower_high,
        "lower_low": f.lower_low,
        "break_below_swing_low": f.break_below_swing_low,
    }
    if cfg.use_prior_day_low_break:
        flags["break_prior_day_low"] = f.break_prior_day_low
    if cfg.require_atr_pct:
        flags["atr_ok"] = f.atr_ok
    return flags


def _bullish_confirm_flags(f: BarFeatures, cfg: DirectionGateConfig) -> dict[str, bool]:
    flags = {
        "ema20_gt_ema59": f.ema20_gt_ema59,
        "ema59_slope_pos": f.ema59_slope_pos,
        "di_bullish": f.di_bullish,
        "adx_ok": f.adx_ok,
        "higher_high": f.higher_high,
        "higher_low": f.higher_low,
        "break_above_swing_high": f.break_above_swing_high,
    }
    if cfg.use_prior_day_high_break:
        flags["break_prior_day_high"] = f.break_prior_day_high
    if cfg.require_atr_pct:
        flags["atr_ok"] = f.atr_ok
    return flags


def evaluate_entry_scores(
    f: BarFeatures,
    cfg: DirectionGateConfig,
) -> dict[str, Any]:
    """Score bearish/bullish entry per variant. Transparent required + confirms."""
    empty = {
        "bearish_entry": False,
        "bullish_entry": False,
        "bearish_score": 0,
        "bullish_score": 0,
        "bearish_required_ok": False,
        "bullish_required_ok": False,
        "bearish_required": {},
        "bullish_required": {},
        "bearish_confirms": {},
        "bullish_confirms": {},
        "bearish_confirm_count": 0,
        "bullish_confirm_count": 0,
        "bearish_need": 0,
        "bullish_need": 0,
        "reason": "warmup_insufficient",
    }
    if not f.warmup_ok:
        return empty

    variant = cfg.variant
    b_flags = _bearish_confirm_flags(f, cfg)
    u_flags = _bullish_confirm_flags(f, cfg)

    if variant == "B1":
        b_req = {
            "close_lt_ema20": f.close_lt_ema20,
            "ema9_lt_ema20": f.ema9_lt_ema20,
            "ema20_slope_neg": f.ema20_slope_neg,
        }
        b_need, b_pool = 2, b_flags
        u_req = {
            "close_gt_ema20": f.close_gt_ema20,
            "ema9_gt_ema20": f.ema9_gt_ema20,
            "ema20_slope_pos": f.ema20_slope_pos,
        }
        u_need, u_pool = 2, u_flags
    elif variant == "B2":
        # Lower High OR clear bearish structure (LH+LL); plus break under swing-low.
        b_req = {
            "structure_ok": bool(f.lower_high or (f.lower_high and f.lower_low)),
            "break_below_swing_low": f.break_below_swing_low,
        }
        b_need = 2
        b_pool = {
            "close_lt_ema20": f.close_lt_ema20,
            "ema9_lt_ema20": f.ema9_lt_ema20,
            "ema20_lt_ema59": f.ema20_lt_ema59,
            "ema20_slope_neg": f.ema20_slope_neg,
            "ema59_slope_neg": f.ema59_slope_neg,
            "di_bearish": f.di_bearish,
            "adx_ok": f.adx_ok,
        }
        u_req = {
            "structure_ok": bool(f.higher_low or (f.higher_low and f.higher_high)),
            "break_above_swing_high": f.break_above_swing_high,
        }
        u_need = 2
        u_pool = {
            "close_gt_ema20": f.close_gt_ema20,
            "ema9_gt_ema20": f.ema9_gt_ema20,
            "ema20_gt_ema59": f.ema20_gt_ema59,
            "ema20_slope_pos": f.ema20_slope_pos,
            "ema59_slope_pos": f.ema59_slope_pos,
            "di_bullish": f.di_bullish,
            "adx_ok": f.adx_ok,
        }
    elif variant == "B3":
        b_req = {
            "close_lt_ema20": f.close_lt_ema20,
            "ema9_lt_ema20_lt_ema59": f.ema9_lt_ema20_lt_ema59,
            "ema20_slope_neg": f.ema20_slope_neg,
            "ema59_slope_neg": f.ema59_slope_neg,
        }
        b_need = 1
        b_pool = {
            "lower_high": f.lower_high,
            "lower_low": f.lower_low,
            "break_below_swing_low": f.break_below_swing_low,
        }
        u_req = {
            "close_gt_ema20": f.close_gt_ema20,
            "ema9_gt_ema20_gt_ema59": f.ema9_gt_ema20_gt_ema59,
            "ema20_slope_pos": f.ema20_slope_pos,
            "ema59_slope_pos": f.ema59_slope_pos,
        }
        u_need = 1
        u_pool = {
            "higher_high": f.higher_high,
            "higher_low": f.higher_low,
            "break_above_swing_high": f.break_above_swing_high,
        }
    else:
        raise ValueError(f"unknown variant {variant}")

    b_req_ok = all(b_req.values())
    u_req_ok = all(u_req.values())
    b_count = sum(1 for v in b_pool.values() if v)
    u_count = sum(1 for v in u_pool.values() if v)

    return {
        "bearish_entry": b_req_ok and b_count >= b_need,
        "bullish_entry": u_req_ok and u_count >= u_need,
        "bearish_score": int(b_req_ok) * 10 + b_count,
        "bullish_score": int(u_req_ok) * 10 + u_count,
        "bearish_required_ok": b_req_ok,
        "bullish_required_ok": u_req_ok,
        "bearish_required": b_req,
        "bullish_required": u_req,
        "bearish_confirms": b_pool,
        "bullish_confirms": u_pool,
        "bearish_confirm_count": b_count,
        "bullish_confirm_count": u_count,
        "bearish_need": b_need,
        "bullish_need": u_need,
        "reason": None,
    }


def _flip_entry(score: dict[str, Any], side: str, cfg: DirectionGateConfig) -> bool:
    """Higher bar for direct opposite flip."""
    if side == "bullish":
        if not score["bullish_required_ok"]:
            return False
        return score["bullish_confirm_count"] >= score["bullish_need"] + cfg.flip_extra_confirmations
    if not score["bearish_required_ok"]:
        return False
    return score["bearish_confirm_count"] >= score["bearish_need"] + cfg.flip_extra_confirmations


def _update_consec(rt: GateRuntimeState, f: BarFeatures) -> None:
    if f.close_gt_ema20:
        rt.consec_close_above_ema20 += 1
        rt.consec_close_below_ema20 = 0
    elif f.close_lt_ema20:
        rt.consec_close_below_ema20 += 1
        rt.consec_close_above_ema20 = 0
    else:
        rt.consec_close_above_ema20 = 0
        rt.consec_close_below_ema20 = 0

    if f.ema_9 is not None and f.ema_20 is not None:
        if f.ema_9 >= f.ema_20:
            rt.consec_ema9_ge_ema20 += 1
            rt.consec_ema9_le_ema20 = 0
        else:
            rt.consec_ema9_le_ema20 += 1
            rt.consec_ema9_ge_ema20 = 0


def _bearish_structure_exit(f: BarFeatures, rt: GateRuntimeState) -> bool:
    """Higher Low + break above last confirmed Lower High."""
    if not f.higher_low:
        return False
    level = rt.last_lower_high_price
    if level is None and f.last_swing_high is not None and f.lower_high is False:
        # fallback: break above last swing high while HL present
        level = f.last_swing_high
    if level is None or f.close is None:
        return False
    return f.close > float(level)


def _bullish_structure_exit(f: BarFeatures, rt: GateRuntimeState) -> bool:
    if not f.lower_high:
        return False
    level = rt.last_higher_low_price
    if level is None:
        level = f.last_swing_low
    if level is None or f.close is None:
        return False
    return f.close < float(level)


def step_gate(
    rt: GateRuntimeState,
    f: BarFeatures,
    score: dict[str, Any],
    cfg: DirectionGateConfig,
) -> tuple[GateRuntimeState, dict[str, Any]]:
    """Advance gate one closed 15m bar. Returns (new_state, event_meta)."""
    meta: dict[str, Any] = {
        "state_before": rt.state,
        "entry_reason": None,
        "exit_reason": None,
        "transition": None,
    }
    if not f.warmup_ok:
        rt.state = "unavailable"
        rt.age_bars = 0
        rt.entered_at = None
        meta["transition"] = "unavailable"
        return rt, meta

    # Track structure levels while observed
    if f.lower_high and f.last_swing_high is not None:
        rt.last_lower_high_price = f.last_swing_high
    if f.higher_low and f.last_swing_low is not None:
        rt.last_higher_low_price = f.last_swing_low

    _update_consec(rt, f)
    prev = rt.state

    # --- entries from neutral/unavailable ---
    if prev in {"neutral", "unavailable"}:
        if score["bearish_entry"]:
            rt.state = "strong_bearish"
            rt.entered_at = f.bar_close_time
            rt.age_bars = 1
            rt.entry_reason = f"bearish_entry_{cfg.variant}"
            rt.exit_reason = None
            meta["entry_reason"] = rt.entry_reason
            meta["transition"] = f"{prev}->strong_bearish"
            return rt, meta
        if score["bullish_entry"]:
            rt.state = "strong_bullish"
            rt.entered_at = f.bar_close_time
            rt.age_bars = 1
            rt.entry_reason = f"bullish_entry_{cfg.variant}"
            rt.exit_reason = None
            meta["entry_reason"] = rt.entry_reason
            meta["transition"] = f"{prev}->strong_bullish"
            return rt, meta
        rt.state = "neutral"
        rt.age_bars = rt.age_bars + 1 if prev == "neutral" else 0
        if prev == "unavailable":
            rt.entered_at = f.bar_close_time
        meta["transition"] = f"{prev}->neutral" if prev != "neutral" else None
        return rt, meta

    # --- hold / exit strong_bearish ---
    if prev == "strong_bearish":
        rt.age_bars += 1
        can_exit = rt.age_bars >= cfg.min_hold_bars

        # Direct flip
        if can_exit and _flip_entry(score, "bullish", cfg):
            rt.state = "strong_bullish"
            rt.entered_at = f.bar_close_time
            rt.age_bars = 1
            rt.exit_reason = "direct_flip_bullish"
            rt.entry_reason = f"bullish_flip_{cfg.variant}"
            meta["exit_reason"] = rt.exit_reason
            meta["entry_reason"] = rt.entry_reason
            meta["transition"] = "strong_bearish->strong_bullish"
            return rt, meta

        exit_reasons = []
        if can_exit and rt.consec_close_above_ema20 >= cfg.exit_closes_beyond_ema20:
            exit_reasons.append("two_closes_above_ema20")
        if can_exit and rt.consec_ema9_ge_ema20 >= cfg.exit_ema9_cross_bars:
            exit_reasons.append("ema9_ge_ema20_two_bars")
        if can_exit and _bearish_structure_exit(f, rt):
            exit_reasons.append("higher_low_break_above_lh")

        if exit_reasons:
            rt.state = "neutral"
            rt.exit_reason = "+".join(exit_reasons)
            rt.entered_at = f.bar_close_time
            rt.age_bars = 0
            meta["exit_reason"] = rt.exit_reason
            meta["transition"] = "strong_bearish->neutral"
            return rt, meta

        meta["transition"] = None
        return rt, meta

    # --- hold / exit strong_bullish (mirror) ---
    if prev == "strong_bullish":
        rt.age_bars += 1
        can_exit = rt.age_bars >= cfg.min_hold_bars

        if can_exit and _flip_entry(score, "bearish", cfg):
            rt.state = "strong_bearish"
            rt.entered_at = f.bar_close_time
            rt.age_bars = 1
            rt.exit_reason = "direct_flip_bearish"
            rt.entry_reason = f"bearish_flip_{cfg.variant}"
            meta["exit_reason"] = rt.exit_reason
            meta["entry_reason"] = rt.entry_reason
            meta["transition"] = "strong_bullish->strong_bearish"
            return rt, meta

        exit_reasons = []
        if can_exit and rt.consec_close_below_ema20 >= cfg.exit_closes_beyond_ema20:
            exit_reasons.append("two_closes_below_ema20")
        if can_exit and rt.consec_ema9_le_ema20 >= cfg.exit_ema9_cross_bars:
            exit_reasons.append("ema9_le_ema20_two_bars")
        if can_exit and _bullish_structure_exit(f, rt):
            exit_reasons.append("lower_high_break_below_hl")

        if exit_reasons:
            rt.state = "neutral"
            rt.exit_reason = "+".join(exit_reasons)
            rt.entered_at = f.bar_close_time
            rt.age_bars = 0
            meta["exit_reason"] = rt.exit_reason
            meta["transition"] = "strong_bullish->neutral"
            return rt, meta

        meta["transition"] = None
        return rt, meta

    return rt, meta


def would_block(state: GateState, side: str) -> bool:
    if state == "strong_bearish" and side == "long":
        return True
    if state == "strong_bullish" and side == "short":
        return True
    return False


def build_15m_indicator_frame(
    candles_5m: pd.DataFrame,
    decision_time: object,
    *,
    scanner_cfg: RegimeScannerConfig | None = None,
) -> tuple[pd.DataFrame, RegimeScannerConfig]:
    """Causal 15m OHLCV + indicators as of decision_time."""
    cfg = scanner_cfg or default_regime_scanner_config()
    tf_cfg = cfg.with_timeframe("15m")
    bars = aggregate_candles(candles_5m, "15m", decision_time)
    if bars.empty:
        return bars, tf_cfg
    frame = compute_indicator_frame(bars, config=tf_cfg)
    return frame, tf_cfg


def prior_day_levels(
    candles_5m: pd.DataFrame,
    day: pd.Timestamp,
) -> tuple[float | None, float | None]:
    """UTC prior calendar day high/low from 5m bars (causal research signal)."""
    day = pd.Timestamp(day)
    if day.tzinfo is None:
        day = day.tz_localize("UTC")
    else:
        day = day.tz_convert("UTC")
    start = (day.normalize() - pd.Timedelta(days=1))
    end = day.normalize()
    c = candles_5m.copy()
    c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
    w = c[(c["timestamp"] >= start) & (c["timestamp"] < end)]
    if w.empty:
        return None, None
    return float(w["low"].min()), float(w["high"].max())


def run_gate_on_15m_frame(
    frame: pd.DataFrame,
    candles_5m: pd.DataFrame,
    cfg: DirectionGateConfig,
    scanner_cfg: RegimeScannerConfig,
    *,
    start_close_time: object | None = None,
    end_close_time: object | None = None,
) -> pd.DataFrame:
    """Walk closed 15m bars and emit one row per bar with state + criteria."""
    if frame.empty:
        return pd.DataFrame()

    pivots = find_confirmed_pivots(frame, config=scanner_cfg)
    rt = GateRuntimeState()
    rows: list[dict[str, Any]] = []
    start_ts = pd.Timestamp(start_close_time, tz="UTC") if start_close_time else None
    end_ts = pd.Timestamp(end_close_time, tz="UTC") if end_close_time else None

    # Precompute prior-day levels cache by date
    level_cache: dict[str, tuple[float | None, float | None]] = {}

    for i in range(len(frame)):
        ts = pd.Timestamp(frame.iloc[i]["timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        close_time = ts + pd.Timedelta(minutes=15)
        if start_ts is not None and close_time < start_ts:
            # still need to warm state from history before window
            pass
        if end_ts is not None and close_time > end_ts:
            break

        day_key = str(ts.normalize().date())
        if day_key not in level_cache:
            level_cache[day_key] = prior_day_levels(candles_5m, ts)
        pdl, pdh = level_cache[day_key]

        feat = compute_bar_features(
            frame,
            i,
            pivots,
            cfg=cfg,
            scanner_cfg=scanner_cfg,
            prior_day_low=pdl,
            prior_day_high=pdh,
        )
        score = evaluate_entry_scores(feat, cfg)
        rt, meta = step_gate(rt, feat, score, cfg)
        rt.last_15m_open = feat.bar_open

        if start_ts is not None and close_time < start_ts:
            continue

        row = {
            **feat.to_dict(),
            "gate_variant": cfg.variant,
            "direction_gate_state": rt.state,
            "state_age_bars": rt.age_bars,
            "state_entered_at": rt.entered_at,
            "entry_reason": meta.get("entry_reason") or rt.entry_reason,
            "exit_reason": meta.get("exit_reason"),
            "transition": meta.get("transition"),
            "bearish_entry_score": score.get("bearish_score"),
            "bullish_entry_score": score.get("bullish_score"),
            "bearish_entry": score.get("bearish_entry"),
            "bullish_entry": score.get("bullish_entry"),
            "bearish_required_ok": score.get("bearish_required_ok"),
            "bullish_required_ok": score.get("bullish_required_ok"),
            "bearish_confirm_count": score.get("bearish_confirm_count"),
            "bullish_confirm_count": score.get("bullish_confirm_count"),
            "bearish_confirms_json": str(score.get("bearish_confirms")),
            "bullish_confirms_json": str(score.get("bullish_confirms")),
            "bearish_required_json": str(score.get("bearish_required")),
            "bullish_required_json": str(score.get("bullish_required")),
            "would_block_long": would_block(rt.state, "long"),
            "would_block_short": would_block(rt.state, "short"),
            "gate_enabled_flag": cfg.enabled,
        }
        rows.append(row)

    return pd.DataFrame(rows)


def expand_15m_state_to_5m_decisions(
    gate_15m: pd.DataFrame,
    decision_times: pd.Series,
) -> pd.DataFrame:
    """Map each 5m decision_time to the last closed 15m gate row (same state within bar)."""
    if gate_15m.empty:
        out = pd.DataFrame({"decision_time": pd.to_datetime(decision_times, utc=True)})
        out["direction_gate_state"] = "unavailable"
        out["would_block_long"] = False
        out["would_block_short"] = False
        return out

    g = gate_15m.copy()
    g["bar_close_time"] = pd.to_datetime(g["bar_close_time"], utc=True)
    g = g.sort_values("bar_close_time")
    dec = pd.DataFrame({"decision_time": pd.to_datetime(decision_times, utc=True)})
    dec = dec.sort_values("decision_time")
    merged = pd.merge_asof(
        dec,
        g,
        left_on="decision_time",
        right_on="bar_close_time",
        direction="backward",
    )
    return merged


def assert_outcomes_do_not_affect_gate(
    gate_rows: pd.DataFrame,
    outcomes: pd.DataFrame | None,
) -> None:
    """Safety: outcomes must not be inputs; this is a documentation/assert helper."""
    if outcomes is None:
        return
    forbidden = {"mfe", "mae", "tp_hit", "deepest_drop", "pnl", "returned_to_signal"}
    overlap = forbidden.intersection(set(c.lower() for c in gate_rows.columns))
    # Outcome-like names should not appear as gate computation inputs in the frame
    # (audit may join later into a separate file).
    assert not overlap, f"gate frame unexpectedly contains outcome columns: {overlap}"
