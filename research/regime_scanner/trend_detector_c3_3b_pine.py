"""Phase C3.3B combined trend-detector Pine export (research-only).

Generates causal as-of Pine visualization and a separate RETRO outcome-audit
script. Does not modify production regime classification or live configs.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence, TYPE_CHECKING

import numpy as np
import pandas as pd

from research.regime_scanner.trend_pine_export import (
    AUDIT_ANCHOR_PLOT,
    build_pine_header,
    pine_escape,
    validate_pine_script,
)

if TYPE_CHECKING:
    from research.regime_scanner.indicator_pattern_discovery_c3_3b import (
        PatternDiscoveryC33BConfig,
    )

ASOF_PINE_NAME = "indicator_combined_trend_detector_asof.pine"
OUTCOME_PINE_NAME = "indicator_combined_trend_detector_outcome_audit.pine"

TREND_STATES: tuple[str, ...] = (
    "neutral",
    "early_bullish",
    "early_bearish",
    "developing_bullish",
    "developing_bearish",
    "confirmed_bullish",
    "confirmed_bearish",
    "weakening_bullish",
    "weakening_bearish",
    "failed_bullish",
    "failed_bearish",
)

STATE_CODE: dict[str, int] = {name: idx for idx, name in enumerate(TREND_STATES)}

BULL_ATTEMPT = {"early_bullish", "developing_bullish", "confirmed_bullish", "weakening_bullish"}
BEAR_ATTEMPT = {"early_bearish", "developing_bearish", "confirmed_bearish", "weakening_bearish"}
BULL_PRE_CONFIRM = {"early_bullish", "developing_bullish"}
BEAR_PRE_CONFIRM = {"early_bearish", "developing_bearish"}


def _finite(value: object, default: float = float("nan")) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _default_cfg() -> PatternDiscoveryC33BConfig:
    from research.regime_scanner.indicator_pattern_discovery_c3_3b import (
        PatternDiscoveryC33BConfig,
    )

    return PatternDiscoveryC33BConfig()


def _threshold_header_lines(cfg: PatternDiscoveryC33BConfig) -> list[str]:
    d = cfg.to_dict()
    lines = [
        "// =============================================================================",
        "// Phase C3.3B combined trend detector — RESEARCH ONLY",
        "// No TradingView strategy declaration, no entries/exits, no production/regime policy.",
        "// Thresholds copied from PatternDiscoveryC33BConfig (do not chart-optimize).",
        "// =============================================================================",
    ]
    keys = [
        "di_spread_expand_min",
        "adx_level_confirmation_min",
        "adx_level_strong_min",
        "adx_rising_min_delta_1",
        "adx_accel_min",
        "ema_flat_slope_max_atr",
        "ema_joint_slope_min_atr",
        "band_expand_min_change_atr",
        "compression_max",
        "near_ema59_atr",
        "near_ema200_atr",
        "di_follow_window_max",
    ]
    for key in keys:
        lines.append(f"// {key} = {d[key]}")
    lines.append("// Indicator periods: EMA 9/20/59/200, DMI/ADX 14, ATR 14, slope window 3")
    lines.append("")
    return lines


def compute_asof_components(
    frame: pd.DataFrame,
    cfg: PatternDiscoveryC33BConfig,
) -> pd.DataFrame:
    """Causal component frame; no future look-ahead."""
    from research.regime_scanner.indicator_pattern_discovery_c3_3b import (
        enrich_discovery_frame,
    )

    out = frame.copy().reset_index(drop=True)
    if "adx_delta_1" not in out.columns:
        out = enrich_discovery_frame(out, cfg)

    plus = pd.to_numeric(out.get("plus_di_14"), errors="coerce").astype("float64")
    minus = pd.to_numeric(out.get("minus_di_14"), errors="coerce").astype("float64")
    di_spread = pd.to_numeric(out.get("di_spread"), errors="coerce").astype("float64")
    if di_spread.isna().all():
        di_spread = plus - minus
        out["di_spread"] = di_spread

    adx = pd.to_numeric(out.get("adx_14"), errors="coerce").astype("float64")
    atr = pd.to_numeric(out.get("atr_14"), errors="coerce").astype("float64")
    ema9 = pd.to_numeric(out.get("ema_9"), errors="coerce").astype("float64")
    ema20 = pd.to_numeric(out.get("ema_20"), errors="coerce").astype("float64")
    close = pd.to_numeric(out.get("close"), errors="coerce").astype("float64")
    band_abs = (ema9 - ema20).abs() / atr.replace(0.0, np.nan)

    out["di_bull"] = plus > minus
    out["di_bear"] = plus < minus
    prev_spread = di_spread.shift(1)
    out["di_cross_bull"] = (prev_spread <= 0) & (di_spread > 0)
    out["di_cross_bear"] = (prev_spread >= 0) & (di_spread < 0)
    out["di_abs"] = di_spread.abs()
    out["di_abs_change_1"] = out["di_abs"] - out["di_abs"].shift(1)
    out["di_expanding"] = out["di_abs_change_1"] >= cfg.di_spread_expand_min
    out["di_shrinking"] = out["di_abs_change_1"] <= -cfg.di_spread_expand_min

    out["adx_delta_1"] = adx - adx.shift(1)
    out["adx_slope_lin_3"] = (adx - adx.shift(3)) / 3.0
    out["adx_slope_lin_5"] = (adx - adx.shift(5)) / 5.0
    out["adx_rising"] = out["adx_delta_1"] >= cfg.adx_rising_min_delta_1
    out["adx_falling"] = out["adx_delta_1"] <= -cfg.adx_rising_min_delta_1
    out["adx_level_ok"] = adx >= cfg.adx_level_confirmation_min
    out["adx_confirm"] = out["adx_level_ok"] | out["adx_rising"]

    out["ema_bull_order"] = ema9 > ema20
    out["ema_bear_order"] = ema9 < ema20
    prev_order = np.sign((ema9 - ema20).shift(1))
    cur_order = np.sign(ema9 - ema20)
    out["ema_cross_bull"] = (prev_order <= 0) & (cur_order > 0)
    out["ema_cross_bear"] = (prev_order >= 0) & (cur_order < 0)

    slope9 = pd.to_numeric(out.get("ema_9_slope_3_atr"), errors="coerce")
    slope20 = pd.to_numeric(out.get("ema_20_slope_3_atr"), errors="coerce")
    if slope9.isna().all():
        slope9 = (ema9 - ema9.shift(3)) / atr
        slope20 = (ema20 - ema20.shift(3)) / atr
        out["ema_9_slope_3_atr"] = slope9
        out["ema_20_slope_3_atr"] = slope20

    out["band_abs_atr"] = band_abs
    out["band_change_3_atr"] = band_abs - band_abs.shift(3)
    out["band_expand"] = out["band_change_3_atr"] >= cfg.band_expand_min_change_atr
    out["band_compress"] = out["band_change_3_atr"] <= -cfg.band_expand_min_change_atr
    prev_expand = out["band_expand"].shift(1).fillna(False).astype(bool)
    out["band_expand_start"] = out["band_expand"].astype(bool) & (~prev_expand)

    joint = (slope9 + slope20) / 2.0
    out["joint_slope"] = joint
    out["joint_rising"] = joint >= cfg.ema_joint_slope_min_atr
    out["joint_falling"] = joint <= -cfg.ema_joint_slope_min_atr
    out["fast_rising"] = slope9 >= cfg.ema_joint_slope_min_atr
    out["fast_falling"] = slope9 <= -cfg.ema_joint_slope_min_atr
    out["slow_rising"] = slope20 >= cfg.ema_flat_slope_max_atr
    out["slow_falling"] = slope20 <= -cfg.ema_flat_slope_max_atr
    out["fast_weakening_from_up"] = slope9 < slope9.shift(1)

    out["price_above_ema59"] = close > pd.to_numeric(out.get("ema_59"), errors="coerce")
    out["price_below_ema59"] = close < pd.to_numeric(out.get("ema_59"), errors="coerce")
    out["price_above_ema200"] = close > pd.to_numeric(out.get("ema_200"), errors="coerce")
    out["price_below_ema200"] = close < pd.to_numeric(out.get("ema_200"), errors="coerce")
    out["move_relevant"] = band_abs >= cfg.band_expand_min_change_atr

    # Research score components (transparent, not a trading rule).
    bull_parts = {
        "bull_di_lead": out["di_bull"],
        "bull_di_pos_spread": di_spread > 0,
        "bull_adx_rising": out["adx_rising"],
        "bull_adx_level": out["adx_level_ok"],
        "bull_fast_slope": slope9 > 0,
        "bull_slow_slope": slope20 > 0,
        "bull_ema_order": out["ema_bull_order"],
        "bull_band_expand": out["band_expand"],
        "bull_above_59": out["price_above_ema59"].fillna(False),
        "bull_above_200": out["price_above_ema200"].fillna(False),
        "bull_move_relevant": out["move_relevant"].fillna(False),
    }
    bear_parts = {
        "bear_di_lead": out["di_bear"],
        "bear_di_neg_spread": di_spread < 0,
        "bear_adx_rising": out["adx_rising"],
        "bear_adx_level": out["adx_level_ok"],
        "bear_fast_slope": slope9 < 0,
        "bear_slow_slope": slope20 < 0,
        "bear_ema_order": out["ema_bear_order"],
        "bear_band_expand": out["band_expand"],
        "bear_below_59": out["price_below_ema59"].fillna(False),
        "bear_below_200": out["price_below_ema200"].fillna(False),
        "bear_move_relevant": out["move_relevant"].fillna(False),
    }
    for name, series in bull_parts.items():
        out[name] = series.fillna(False).astype(bool)
    for name, series in bear_parts.items():
        out[name] = series.fillna(False).astype(bool)
    out["bullish_score"] = sum(out[k].astype(int) for k in bull_parts)
    out["bearish_score"] = sum(out[k].astype(int) for k in bear_parts)
    out["net_score"] = out["bullish_score"] - out["bearish_score"]
    out["bullish_component_count"] = out["bullish_score"]
    out["bearish_component_count"] = out["bearish_score"]
    return out


def classify_asof_state(
    row: Mapping[str, Any],
    *,
    prev_state: str,
    cfg: PatternDiscoveryC33BConfig,
) -> str:
    """Causal state from current as-of components + previous state only."""
    _ = cfg  # thresholds already baked into boolean components
    di_cross_bull = bool(row.get("di_cross_bull"))
    di_cross_bear = bool(row.get("di_cross_bear"))
    di_bull = bool(row.get("di_bull"))
    di_bear = bool(row.get("di_bear"))
    ema_bull = bool(row.get("ema_bull_order"))
    ema_bear = bool(row.get("ema_bear_order"))
    adx_rising = bool(row.get("adx_rising"))
    adx_falling = bool(row.get("adx_falling"))
    adx_confirm = bool(row.get("adx_confirm"))
    joint_rising = bool(row.get("joint_rising"))
    joint_falling = bool(row.get("joint_falling"))
    band_expand = bool(row.get("band_expand"))
    band_compress = bool(row.get("band_compress"))
    di_shrinking = bool(row.get("di_shrinking"))
    di_expanding = bool(row.get("di_expanding"))
    fast_weakening = bool(row.get("fast_weakening_from_up"))
    move_ok = bool(row.get("move_relevant"))
    slope9 = _finite(row.get("ema_9_slope_3_atr"), 0.0)
    slope20 = _finite(row.get("ema_20_slope_3_atr"), 0.0)
    adx_slope3 = _finite(row.get("adx_slope_lin_3"), 0.0)

    # Failed: prior early/developing attempt collapses without future look-ahead.
    if prev_state in BULL_PRE_CONFIRM and (
        di_cross_bear or (di_bear and not ema_bull)
    ):
        return "failed_bullish"
    if prev_state in BEAR_PRE_CONFIRM and (
        di_cross_bull or (di_bull and not ema_bear)
    ):
        return "failed_bearish"

    confirmed_bull = (
        ema_bull
        and slope9 > 0
        and slope20 > 0
        and band_expand
        and di_bull
        and adx_confirm
        and move_ok
    )
    confirmed_bear = (
        ema_bear
        and slope9 < 0
        and slope20 < 0
        and band_expand
        and di_bear
        and adx_confirm
        and move_ok
    )
    if confirmed_bull:
        return "confirmed_bullish"
    if confirmed_bear:
        return "confirmed_bearish"

    weakening_bull = (
        ema_bull
        and prev_state in BULL_ATTEMPT
        and adx_falling
        and (di_shrinking or band_compress or fast_weakening)
    )
    slope9_prev = _finite(row.get("ema_9_slope_3_atr_prev"), slope9)
    fast_less_bearish = slope9 > slope9_prev  # bearish slope becoming less negative / flipping
    weakening_bear = (
        ema_bear
        and prev_state in BEAR_ATTEMPT
        and adx_falling
        and (di_shrinking or band_compress or fast_less_bearish)
    )
    if weakening_bull:
        return "weakening_bullish"
    if weakening_bear:
        return "weakening_bearish"

    developing_bull = (
        di_bull
        and (adx_rising or adx_slope3 > 0)
        and joint_rising
        and (band_expand or di_expanding)
        and not confirmed_bull
    )
    developing_bear = (
        di_bear
        and (adx_rising or adx_slope3 > 0)
        and joint_falling
        and (band_expand or di_expanding)
        and not confirmed_bear
    )
    if developing_bull:
        return "developing_bullish"
    if developing_bear:
        return "developing_bearish"

    early_bull = di_cross_bull or (di_bull and di_expanding and not ema_bull)
    early_bear = di_cross_bear or (di_bear and di_expanding and not ema_bear)
    if early_bull:
        return "early_bullish"
    if early_bear:
        return "early_bearish"

    if prev_state in {"failed_bullish", "failed_bearish"}:
        return "neutral"
    return "neutral"


def compute_trend_detector_states(
    frame: pd.DataFrame,
    cfg: PatternDiscoveryC33BConfig | None = None,
) -> pd.DataFrame:
    cfg = cfg or _default_cfg()
    comps = compute_asof_components(frame, cfg)
    # Prior fast slope for bear weakening.
    comps["ema_9_slope_3_atr_prev"] = comps["ema_9_slope_3_atr"].shift(1)

    states: list[str] = []
    prev = "neutral"
    for i in range(len(comps)):
        row = comps.iloc[i].to_dict()
        state = classify_asof_state(row, prev_state=prev, cfg=cfg)
        states.append(state)
        prev = state
    comps["research_state"] = states
    comps["research_state_code"] = [STATE_CODE[s] for s in states]
    # Explicit: no retro columns participate in research_state.
    comps["uses_future_lookahead"] = False
    comps["retro_influences_state"] = False
    return comps


def summarize_state_counts(states: Sequence[str]) -> list[dict[str, Any]]:
    counts = Counter(states)
    total = len(states) or 1
    return [
        {
            "state": state,
            "n_bars": int(counts.get(state, 0)),
            "share": float(counts.get(state, 0) / total),
        }
        for state in TREND_STATES
    ]


def summarize_transitions(states: Sequence[str]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    for a, b in zip(states, states[1:]):
        if a != b:
            counts[(a, b)] += 1
    rows = [
        {"from_state": a, "to_state": b, "n_transitions": n}
        for (a, b), n in counts.items()
    ]
    return sorted(rows, key=lambda r: (-int(r["n_transitions"]), r["from_state"], r["to_state"]))


def summarize_components(frame: pd.DataFrame) -> list[dict[str, Any]]:
    cols = [c for c in frame.columns if c.startswith(("bull_", "bear_"))]
    rows: list[dict[str, Any]] = []
    for col in sorted(cols):
        series = frame[col].fillna(False).astype(bool)
        rows.append(
            {
                "component": col,
                "n_true": int(series.sum()),
                "share_true": float(series.mean()) if len(series) else 0.0,
            }
        )
    return rows


def transition_rates(states: Sequence[str]) -> dict[str, float | None]:
    early_bull = sum(1 for s in states if s == "early_bullish")
    early_bear = sum(1 for s in states if s == "early_bearish")
    developing_bull = sum(1 for s in states if s == "developing_bullish")
    developing_bear = sum(1 for s in states if s == "developing_bearish")

    e2d = sum(
        1
        for a, b in zip(states, states[1:])
        if a in {"early_bullish", "early_bearish"}
        and b in {"developing_bullish", "developing_bearish"}
    )
    d2c = sum(
        1
        for a, b in zip(states, states[1:])
        if a in {"developing_bullish", "developing_bearish"}
        and b in {"confirmed_bullish", "confirmed_bearish"}
    )
    e_or_d_to_fail = sum(
        1
        for a, b in zip(states, states[1:])
        if a in BULL_PRE_CONFIRM | BEAR_PRE_CONFIRM
        and b in {"failed_bullish", "failed_bearish"}
    )
    early_n = early_bull + early_bear
    developing_n = developing_bull + developing_bear
    pre_n = sum(1 for s in states if s in BULL_PRE_CONFIRM | BEAR_PRE_CONFIRM)
    return {
        "early_to_developing_share": (e2d / early_n) if early_n else None,
        "developing_to_confirmed_share": (d2c / developing_n) if developing_n else None,
        "early_or_developing_to_failed_share": (e_or_d_to_fail / pre_n) if pre_n else None,
        "n_early_to_developing": float(e2d),
        "n_developing_to_confirmed": float(d2c),
        "n_early_or_developing_to_failed": float(e_or_d_to_fail),
    }


def mean_di_to_confirmed_delay(
    frame: pd.DataFrame,
    *,
    max_lookforward: int = 48,
) -> float | None:
    """Research metric: bars from DI-cross to later confirmed (path/audit only)."""
    delays: list[int] = []
    states = frame["research_state"].astype(str).tolist()
    di_bull = frame["di_cross_bull"].fillna(False).astype(bool).tolist()
    di_bear = frame["di_cross_bear"].fillna(False).astype(bool).tolist()
    for i, (bull, bear) in enumerate(zip(di_bull, di_bear)):
        target = None
        if bull:
            target = "confirmed_bullish"
        elif bear:
            target = "confirmed_bearish"
        if target is None:
            continue
        for lag in range(0, max_lookforward + 1):
            j = i + lag
            if j >= len(states):
                break
            if states[j] == target:
                delays.append(lag)
                break
    if not delays:
        return None
    return float(sum(delays) / len(delays))


def _pine_const_block(cfg: PatternDiscoveryC33BConfig) -> list[str]:
    return [
        "// ---- C3.3B thresholds (defaults; chart inputs mirror config) ----",
        f'diSpreadExpandMin = input.float({cfg.di_spread_expand_min}, "DI spread expand min")',
        f'adxConfirmMin = input.float({cfg.adx_level_confirmation_min}, "ADX confirmation level")',
        f'adxRisingMin = input.float({cfg.adx_rising_min_delta_1}, "ADX rising min delta 1")',
        f'emaJointSlopeMin = input.float({cfg.ema_joint_slope_min_atr}, "EMA joint slope min ATR")',
        f'emaFlatSlopeMax = input.float({cfg.ema_flat_slope_max_atr}, "EMA flat slope max ATR")',
        f'bandExpandMin = input.float({cfg.band_expand_min_change_atr}, "Band expand min change ATR")',
        'showEma59 = input.bool(true, "Show EMA59")',
        'showEma200 = input.bool(true, "Show EMA200")',
        'showBg = input.bool(true, "Show state background")',
        'showMarkers = input.bool(true, "Show event markers")',
        'showTable = input.bool(true, "Show diagnostics table")',
        'showScores = input.bool(true, "Show research scores")',
        'grpEarly = input.bool(true, "Markers: DI/EMA crosses", group="Markers")',
        'grpConfirm = input.bool(true, "Markers: confirm/weaken/fail", group="Markers")',
        "",
    ]


def build_asof_pine_script(
    *,
    cfg: PatternDiscoveryC33BConfig | None = None,
    symbol: str = "APTUSDT",
    timeframe: str = "30m",
) -> str:
    cfg = cfg or _default_cfg()
    lines = [
        *build_pine_header("C3.3B Combined Trend Detector (as-of)"),
        *_threshold_header_lines(cfg),
        f"// Symbol/TF intent: {symbol} {timeframe} | causal as-of only",
        "// State rules are documented inline; no future bars used for research_state.",
        "",
        *_pine_const_block(cfg),
        "emaFast = ta.ema(close, 9)",
        "emaSlow = ta.ema(close, 20)",
        "ema59 = ta.ema(close, 59)",
        "ema200 = ta.ema(close, 200)",
        "atr14 = ta.atr(14)",
        "[plusDI, minusDI, adx14] = ta.dmi(14, 14)",
        "",
        "diSpread = plusDI - minusDI",
        "diAbs = math.abs(diSpread)",
        "diAbsChange1 = diAbs - diAbs[1]",
        "diCrossBull = ta.crossover(plusDI, minusDI)",
        "diCrossBear = ta.crossunder(plusDI, minusDI)",
        "diBull = plusDI > minusDI",
        "diBear = plusDI < minusDI",
        "diExpanding = diAbsChange1 >= diSpreadExpandMin",
        "diShrinking = diAbsChange1 <= -diSpreadExpandMin",
        "",
        "adxDelta1 = adx14 - adx14[1]",
        "adxSlope3 = (adx14 - adx14[3]) / 3.0",
        "adxSlope5 = (adx14 - adx14[5]) / 5.0",
        "adxRising = adxDelta1 >= adxRisingMin",
        "adxFalling = adxDelta1 <= -adxRisingMin",
        "adxLevelOk = adx14 >= adxConfirmMin",
        "adxConfirm = adxLevelOk or adxRising",
        "",
        "emaBullOrder = emaFast > emaSlow",
        "emaBearOrder = emaFast < emaSlow",
        "emaCrossBull = ta.crossover(emaFast, emaSlow)",
        "emaCrossBear = ta.crossunder(emaFast, emaSlow)",
        "bandAbsAtr = math.abs(emaFast - emaSlow) / atr14",
        "bandChange3 = bandAbsAtr - bandAbsAtr[3]",
        "bandExpand = bandChange3 >= bandExpandMin",
        "bandCompress = bandChange3 <= -bandExpandMin",
        "bandExpandStart = bandExpand and not bandExpand[1]",
        "slopeFast = (emaFast - emaFast[3]) / atr14",
        "slopeSlow = (emaSlow - emaSlow[3]) / atr14",
        "jointSlope = (slopeFast + slopeSlow) / 2.0",
        "jointRising = jointSlope >= emaJointSlopeMin",
        "jointFalling = jointSlope <= -emaJointSlopeMin",
        "fastWeakeningUp = slopeFast < slopeFast[1]",
        "fastStrengtheningFromDown = slopeFast > slopeFast[1]",
        "moveRelevant = bandAbsAtr >= bandExpandMin",
        "",
        "// Research score components (not a trading rule).",
        "bullScore =",
        "     (diBull ? 1 : 0)",
        "   + (diSpread > 0 ? 1 : 0)",
        "   + (adxRising ? 1 : 0)",
        "   + (adxLevelOk ? 1 : 0)",
        "   + (slopeFast > 0 ? 1 : 0)",
        "   + (slopeSlow > 0 ? 1 : 0)",
        "   + (emaBullOrder ? 1 : 0)",
        "   + (bandExpand ? 1 : 0)",
        "   + (close > ema59 ? 1 : 0)",
        "   + (close > ema200 ? 1 : 0)",
        "   + (moveRelevant ? 1 : 0)",
        "bearScore =",
        "     (diBear ? 1 : 0)",
        "   + (diSpread < 0 ? 1 : 0)",
        "   + (adxRising ? 1 : 0)",
        "   + (adxLevelOk ? 1 : 0)",
        "   + (slopeFast < 0 ? 1 : 0)",
        "   + (slopeSlow < 0 ? 1 : 0)",
        "   + (emaBearOrder ? 1 : 0)",
        "   + (bandExpand ? 1 : 0)",
        "   + (close < ema59 ? 1 : 0)",
        "   + (close < ema200 ? 1 : 0)",
        "   + (moveRelevant ? 1 : 0)",
        "netScore = bullScore - bearScore",
        "",
        "confirmedBull = emaBullOrder and slopeFast > 0 and slopeSlow > 0 and bandExpand and diBull and adxConfirm and moveRelevant",
        "confirmedBear = emaBearOrder and slopeFast < 0 and slopeSlow < 0 and bandExpand and diBear and adxConfirm and moveRelevant",
        "developingBull = diBull and (adxRising or adxSlope3 > 0) and jointRising and (bandExpand or diExpanding) and not confirmedBull",
        "developingBear = diBear and (adxRising or adxSlope3 > 0) and jointFalling and (bandExpand or diExpanding) and not confirmedBear",
        "earlyBull = diCrossBull or (diBull and diExpanding and not emaBullOrder)",
        "earlyBear = diCrossBear or (diBear and diExpanding and not emaBearOrder)",
        "",
        'var int stateCode = 0',
        'var string researchState = "neutral"',
        "",
        "// Causal state machine (depends only on current components + previous state).",
        "prevState = researchState[1]",
        'failedBull = (prevState == "early_bullish" or prevState == "developing_bullish") and (diCrossBear or (diBear and not emaBullOrder))',
        'failedBear = (prevState == "early_bearish" or prevState == "developing_bearish") and (diCrossBull or (diBull and not emaBearOrder))',
        'weakeningBull = emaBullOrder and (prevState == "early_bullish" or prevState == "developing_bullish" or prevState == "confirmed_bullish" or prevState == "weakening_bullish") and adxFalling and (diShrinking or bandCompress or fastWeakeningUp)',
        'weakeningBear = emaBearOrder and (prevState == "early_bearish" or prevState == "developing_bearish" or prevState == "confirmed_bearish" or prevState == "weakening_bearish") and adxFalling and (diShrinking or bandCompress or fastStrengtheningFromDown)',
        "",
        "if failedBull",
        '    researchState := "failed_bullish"',
        "    stateCode := 9",
        "else if failedBear",
        '    researchState := "failed_bearish"',
        "    stateCode := 10",
        "else if confirmedBull",
        '    researchState := "confirmed_bullish"',
        "    stateCode := 5",
        "else if confirmedBear",
        '    researchState := "confirmed_bearish"',
        "    stateCode := 6",
        "else if weakeningBull",
        '    researchState := "weakening_bullish"',
        "    stateCode := 7",
        "else if weakeningBear",
        '    researchState := "weakening_bearish"',
        "    stateCode := 8",
        "else if developingBull",
        '    researchState := "developing_bullish"',
        "    stateCode := 3",
        "else if developingBear",
        '    researchState := "developing_bearish"',
        "    stateCode := 4",
        "else if earlyBull",
        '    researchState := "early_bullish"',
        "    stateCode := 1",
        "else if earlyBear",
        '    researchState := "early_bearish"',
        "    stateCode := 2",
        "else",
        '    researchState := "neutral"',
        "    stateCode := 0",
        "",
        'plot(emaFast, "EMA Fast 9", color=color.new(color.teal, 0), linewidth=2)',
        'plot(emaSlow, "EMA Slow 20", color=color.new(color.orange, 0), linewidth=2)',
        'plot(showEma59 ? ema59 : na, "EMA 59", color=color.new(color.purple, 0), linewidth=1)',
        'plot(showEma200 ? ema200 : na, "EMA 200", color=color.new(color.gray, 0), linewidth=1)',
        "",
        'bgcolor(showBg and stateCode == 1 ? color.new(color.lime, 92) : na, title="early_bullish")',
        'bgcolor(showBg and stateCode == 2 ? color.new(color.maroon, 92) : na, title="early_bearish")',
        'bgcolor(showBg and stateCode == 3 ? color.new(color.green, 88) : na, title="developing_bullish")',
        'bgcolor(showBg and stateCode == 4 ? color.new(color.red, 88) : na, title="developing_bearish")',
        'bgcolor(showBg and stateCode == 5 ? color.new(color.aqua, 85) : na, title="confirmed_bullish")',
        'bgcolor(showBg and stateCode == 6 ? color.new(color.fuchsia, 85) : na, title="confirmed_bearish")',
        'bgcolor(showBg and stateCode == 7 ? color.new(color.olive, 90) : na, title="weakening_bullish")',
        'bgcolor(showBg and stateCode == 8 ? color.new(color.orange, 90) : na, title="weakening_bearish")',
        "",
        'plotshape(showMarkers and grpEarly and diCrossBull, title="DI cross bull", style=shape.triangleup, location=location.belowbar, color=color.new(color.lime, 0), size=size.tiny)',
        'plotshape(showMarkers and grpEarly and diCrossBear, title="DI cross bear", style=shape.triangledown, location=location.abovebar, color=color.new(color.red, 0), size=size.tiny)',
        'plotshape(showMarkers and grpEarly and emaCrossBull, title="EMA cross bull", style=shape.circle, location=location.belowbar, color=color.new(color.teal, 0), size=size.tiny)',
        'plotshape(showMarkers and grpEarly and emaCrossBear, title="EMA cross bear", style=shape.circle, location=location.abovebar, color=color.new(color.orange, 0), size=size.tiny)',
        'plotshape(showMarkers and grpEarly and bandExpandStart, title="EMA band expand start", style=shape.diamond, location=location.belowbar, color=color.new(color.yellow, 0), size=size.tiny)',
        'plotshape(showMarkers and grpConfirm and adxConfirm and (confirmedBull or confirmedBear or developingBull or developingBear), title="ADX confirmed", style=shape.xcross, location=location.abovebar, color=color.new(color.yellow, 20), size=size.tiny)',
        'plotshape(showMarkers and grpConfirm and (stateCode == 5 or stateCode == 6), title="confirmed trend", style=shape.flag, location=location.belowbar, color=color.new(color.aqua, 0), size=size.tiny)',
        'plotshape(showMarkers and grpConfirm and (stateCode == 7 or stateCode == 8), title="weakening", style=shape.square, location=location.abovebar, color=color.new(color.olive, 0), size=size.tiny)',
        'plotshape(showMarkers and grpConfirm and (stateCode == 9 or stateCode == 10), title="failed setup", style=shape.xcross, location=location.abovebar, color=color.new(color.black, 0), size=size.tiny)',
        "",
        'plot(showScores ? bullScore : na, "bullish score", color=color.new(color.green, 0), display=display.pane)',
        'plot(showScores ? bearScore : na, "bearish score", color=color.new(color.red, 0), display=display.pane)',
        'plot(showScores ? netScore : na, "net score", color=color.new(color.blue, 0), display=display.pane)',
        "",
        "var table diag = table.new(position.top_right, 2, 16, border_width=1)",
        "if showTable and barstate.islast",
        "    table.cell(diag, 0, 0, \"research_state\")",
        "    table.cell(diag, 1, 0, researchState)",
        '    table.cell(diag, 0, 1, "+DI")',
        "    table.cell(diag, 1, 1, str.tostring(plusDI, \"#.##\"))",
        '    table.cell(diag, 0, 2, "-DI")',
        "    table.cell(diag, 1, 2, str.tostring(minusDI, \"#.##\"))",
        '    table.cell(diag, 0, 3, "DI diff")',
        "    table.cell(diag, 1, 3, str.tostring(diSpread, \"#.##\"))",
        '    table.cell(diag, 0, 4, "ADX")',
        "    table.cell(diag, 1, 4, str.tostring(adx14, \"#.##\"))",
        '    table.cell(diag, 0, 5, "ADX slope3")',
        "    table.cell(diag, 1, 5, str.tostring(adxSlope3, \"#.###\"))",
        '    table.cell(diag, 0, 6, "ADX slope5")',
        "    table.cell(diag, 1, 6, str.tostring(adxSlope5, \"#.###\"))",
        '    table.cell(diag, 0, 7, "ATR")',
        "    table.cell(diag, 1, 7, str.tostring(atr14, \"#.####\"))",
        '    table.cell(diag, 0, 8, "Band ATR")',
        "    table.cell(diag, 1, 8, str.tostring(bandAbsAtr, \"#.###\"))",
        '    table.cell(diag, 0, 9, "Fast slope")',
        "    table.cell(diag, 1, 9, str.tostring(slopeFast, \"#.###\"))",
        '    table.cell(diag, 0, 10, "Slow slope")',
        "    table.cell(diag, 1, 10, str.tostring(slopeSlow, \"#.###\"))",
        '    table.cell(diag, 0, 11, "Bull comps")',
        "    table.cell(diag, 1, 11, str.tostring(bullScore))",
        '    table.cell(diag, 0, 12, "Bear comps")',
        "    table.cell(diag, 1, 12, str.tostring(bearScore))",
        '    table.cell(diag, 0, 13, "Net score")',
        "    table.cell(diag, 1, 13, str.tostring(netScore))",
        "",
        "// EOF",
    ]
    text = "\n".join(lines) + "\n"
    validate_pine_script(text)
    if re.search(r"(?m)^strategy\(", text):
        raise ValueError("as-of pine must not contain strategy(")
    return text


def _limit_markers(markers: Sequence[Mapping[str, Any]], max_count: int = 120) -> list[dict[str, Any]]:
    rows = [dict(m) for m in markers]
    if len(rows) <= max_count:
        return rows
    step = max(1, len(rows) // max_count)
    out = [rows[i] for i in range(0, len(rows), step)][:max_count]
    if out and rows and out[-1] != rows[-1]:
        out[-1] = rows[-1]
    return out


def build_retro_markers(
    *,
    di_ema_sequences: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """RETRO-only markers from C3.3B artifacts. Must not feed as-of state."""
    markers: list[dict[str, Any]] = []
    for seq in di_ema_sequences:
        if str(seq.get("event_type")) != "di_cross":
            continue
        ts = seq.get("event_timestamp") or seq.get("decision_time")
        lag = seq.get("paired_ema_lag_bars")
        bucket = str(seq.get("paired_ema_lag_bucket") or "none")
        if lag is None or bucket == "none":
            label = "RETRO DI no EMA follow"
        elif int(lag) == 0:
            label = "RETRO DI/EMA coincident"
        elif bucket == "1":
            label = "RETRO DI->EMA lag1"
        elif bucket == "2":
            label = "RETRO DI->EMA lag2"
        elif bucket == "3":
            label = "RETRO DI->EMA lag3"
        elif bucket == "4_6":
            label = "RETRO DI->EMA lag4-6"
        elif bucket == "7_12":
            label = "RETRO DI->EMA lag7-12"
        else:
            label = f"RETRO DI->EMA lag{lag}"
        markers.append(
            {
                "event_timestamp": ts,
                "label": label,
                "kind": "retro_sequence",
                "is_retrospective": True,
            }
        )
        if bool(seq.get("di_spread_expanding_after_1_path")) and not bool(
            seq.get("has_ema_band_expansion")
        ):
            markers.append(
                {
                    "event_timestamp": ts,
                    "label": "RETRO band expand after cross",
                    "kind": "retro_band",
                    "is_retrospective": True,
                }
            )

    outcome_labels = {
        "clean_success": "RETRO clean_success",
        "early_adverse_then_recovery": "RETRO early_adverse_then_recovery",
        "delayed_success": "RETRO delayed_success",
        "failed_followthrough": "RETRO failed_followthrough",
        "adverse_reversal": "RETRO adverse_reversal",
    }
    for ev in outcomes:
        cls = str(ev.get("outcome_class") or "")
        if cls not in outcome_labels:
            continue
        if str(ev.get("event_type")) not in {"di_cross", "ema_cross"}:
            continue
        markers.append(
            {
                "event_timestamp": ev.get("event_timestamp"),
                "label": outcome_labels[cls],
                "kind": "retro_outcome",
                "is_retrospective": True,
            }
        )
    markers = [m for m in markers if m.get("event_timestamp") is not None]
    markers.sort(key=lambda m: str(m["event_timestamp"]))
    return _limit_markers(markers, 120)


def build_outcome_audit_pine_script(
    *,
    cfg: PatternDiscoveryC33BConfig | None = None,
    symbol: str = "APTUSDT",
    timeframe: str = "30m",
    retro_markers: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    cfg = cfg or _default_cfg()
    markers = list(retro_markers or [])
    lines = [
        *build_pine_header("C3.3B Combined Trend Detector Outcome Audit"),
        *_threshold_header_lines(cfg),
        f"// Symbol/TF intent: {symbol} {timeframe}",
        "// RETRO markers are historical audit only — NOT live-policy-capable.",
        "// RETRO arrays must never alter researchState / scores below.",
        "",
        *_pine_const_block(cfg),
        'showRetro = input.bool(true, "Show RETRO markers")',
        "",
        "emaFast = ta.ema(close, 9)",
        "emaSlow = ta.ema(close, 20)",
        "ema59 = ta.ema(close, 59)",
        "ema200 = ta.ema(close, 200)",
        "atr14 = ta.atr(14)",
        "[plusDI, minusDI, adx14] = ta.dmi(14, 14)",
        "diSpread = plusDI - minusDI",
        "diCrossBull = ta.crossover(plusDI, minusDI)",
        "diCrossBear = ta.crossunder(plusDI, minusDI)",
        "diBull = plusDI > minusDI",
        "diBear = plusDI < minusDI",
        "diAbsChange1 = math.abs(diSpread) - math.abs(diSpread[1])",
        "diExpanding = diAbsChange1 >= diSpreadExpandMin",
        "adxDelta1 = adx14 - adx14[1]",
        "adxRising = adxDelta1 >= adxRisingMin",
        "adxFalling = adxDelta1 <= -adxRisingMin",
        "adxConfirm = (adx14 >= adxConfirmMin) or adxRising",
        "emaBullOrder = emaFast > emaSlow",
        "emaBearOrder = emaFast < emaSlow",
        "emaCrossBull = ta.crossover(emaFast, emaSlow)",
        "emaCrossBear = ta.crossunder(emaFast, emaSlow)",
        "bandAbsAtr = math.abs(emaFast - emaSlow) / atr14",
        "bandChange3 = bandAbsAtr - bandAbsAtr[3]",
        "bandExpand = bandChange3 >= bandExpandMin",
        "bandCompress = bandChange3 <= -bandExpandMin",
        "slopeFast = (emaFast - emaFast[3]) / atr14",
        "slopeSlow = (emaSlow - emaSlow[3]) / atr14",
        "jointSlope = (slopeFast + slopeSlow) / 2.0",
        "jointRising = jointSlope >= emaJointSlopeMin",
        "jointFalling = jointSlope <= -emaJointSlopeMin",
        "moveRelevant = bandAbsAtr >= bandExpandMin",
        "bullScore = (diBull ? 1 : 0) + (diSpread > 0 ? 1 : 0) + (adxRising ? 1 : 0) + ((adx14 >= adxConfirmMin) ? 1 : 0) + (slopeFast > 0 ? 1 : 0) + (slopeSlow > 0 ? 1 : 0) + (emaBullOrder ? 1 : 0) + (bandExpand ? 1 : 0) + (close > ema59 ? 1 : 0) + (close > ema200 ? 1 : 0) + (moveRelevant ? 1 : 0)",
        "bearScore = (diBear ? 1 : 0) + (diSpread < 0 ? 1 : 0) + (adxRising ? 1 : 0) + ((adx14 >= adxConfirmMin) ? 1 : 0) + (slopeFast < 0 ? 1 : 0) + (slopeSlow < 0 ? 1 : 0) + (emaBearOrder ? 1 : 0) + (bandExpand ? 1 : 0) + (close < ema59 ? 1 : 0) + (close < ema200 ? 1 : 0) + (moveRelevant ? 1 : 0)",
        "netScore = bullScore - bearScore",
        "confirmedBull = emaBullOrder and slopeFast > 0 and slopeSlow > 0 and bandExpand and diBull and adxConfirm and moveRelevant",
        "confirmedBear = emaBearOrder and slopeFast < 0 and slopeSlow < 0 and bandExpand and diBear and adxConfirm and moveRelevant",
        "developingBull = diBull and (adxRising or ((adx14 - adx14[3]) / 3.0) > 0) and jointRising and (bandExpand or diExpanding) and not confirmedBull",
        "developingBear = diBear and (adxRising or ((adx14 - adx14[3]) / 3.0) > 0) and jointFalling and (bandExpand or diExpanding) and not confirmedBear",
        "earlyBull = diCrossBull or (diBull and diExpanding and not emaBullOrder)",
        "earlyBear = diCrossBear or (diBear and diExpanding and not emaBearOrder)",
        'var string researchState = "neutral"',
        "prevState = researchState[1]",
        'failedBull = (prevState == "early_bullish" or prevState == "developing_bullish") and (diCrossBear or (diBear and not emaBullOrder))',
        'failedBear = (prevState == "early_bearish" or prevState == "developing_bearish") and (diCrossBull or (diBull and not emaBearOrder))',
        "if failedBull",
        '    researchState := "failed_bullish"',
        "else if failedBear",
        '    researchState := "failed_bearish"',
        "else if confirmedBull",
        '    researchState := "confirmed_bullish"',
        "else if confirmedBear",
        '    researchState := "confirmed_bearish"',
        "else if developingBull",
        '    researchState := "developing_bullish"',
        "else if developingBear",
        '    researchState := "developing_bearish"',
        "else if earlyBull",
        '    researchState := "early_bullish"',
        "else if earlyBear",
        '    researchState := "early_bearish"',
        "else",
        '    researchState := "neutral"',
        "",
        'plot(emaFast, "EMA Fast 9", color=color.new(color.teal, 0), linewidth=2)',
        'plot(emaSlow, "EMA Slow 20", color=color.new(color.orange, 0), linewidth=2)',
        'plot(showEma59 ? ema59 : na, "EMA 59", color=color.new(color.purple, 0), linewidth=1)',
        'plot(showEma200 ? ema200 : na, "EMA 200", color=color.new(color.gray, 0), linewidth=1)',
        'bgcolor(showBg and researchState == "early_bullish" ? color.new(color.lime, 92) : na)',
        'bgcolor(showBg and researchState == "early_bearish" ? color.new(color.maroon, 92) : na)',
        'bgcolor(showBg and researchState == "developing_bullish" ? color.new(color.green, 88) : na)',
        'bgcolor(showBg and researchState == "developing_bearish" ? color.new(color.red, 88) : na)',
        'bgcolor(showBg and researchState == "confirmed_bullish" ? color.new(color.aqua, 85) : na)',
        'bgcolor(showBg and researchState == "confirmed_bearish" ? color.new(color.fuchsia, 85) : na)',
        "",
        "// ---- RETRO marker arrays (audit only; do not feed researchState) ----",
        "f_ts(y, m, d, h, mi) =>",
        '    timestamp("UTC", y, m, d, h, mi)',
        "",
        "var int[] retroTimes = array.new_int()",
        "var string[] retroLabels = array.new_string()",
        "",
    ]
    for idx, mk in enumerate(markers):
        ts = pd.Timestamp(mk["event_timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        label = pine_escape(str(mk.get("label") or "RETRO"))
        lines.extend(
            [
                f"if barstate.isfirst and {idx} == {idx}",
                f"    array.push(retroTimes, f_ts({ts.year}, {ts.month}, {ts.day}, {ts.hour}, {ts.minute}))",
                f'    array.push(retroLabels, "{label}")',
                "",
            ]
        )
    lines.extend(
        [
            "if showRetro and array.size(retroTimes) > 0",
            "    for i = 0 to array.size(retroTimes) - 1",
            "        if time_close == array.get(retroTimes, i)",
            '            label.new(bar_index, high, array.get(retroLabels, i), style=label.style_label_down, color=color.new(color.gray, 20), textcolor=color.white, size=size.tiny)',
            "",
            'plot(showScores ? netScore : na, "net score", color=color.new(color.blue, 0), display=display.pane)',
            "",
            "// EOF",
        ]
    )
    text = "\n".join(lines) + "\n"
    validate_pine_script(text)
    if re.search(r"(?m)^strategy\(", text):
        raise ValueError("outcome pine must not contain strategy(")
    if "researchState :=" in text[text.index("RETRO marker arrays") :]:
        raise ValueError("RETRO section must not assign researchState")
    return text


def export_trend_detector_artifacts(
    *,
    frame: pd.DataFrame,
    output_dir: Path,
    cfg: PatternDiscoveryC33BConfig | None = None,
    symbol: str = "APTUSDT",
    timeframe: str = "30m",
    analyze_start: str | None = None,
    analyze_end: str | None = None,
    di_ema_sequences: Sequence[Mapping[str, Any]] | None = None,
    outcomes: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    cfg = cfg or _default_cfg()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    states_df = compute_trend_detector_states(frame, cfg)
    if analyze_start is not None and analyze_end is not None and "decision_time" in states_df.columns:
        a0 = pd.Timestamp(analyze_start)
        a1 = pd.Timestamp(analyze_end)
        if a0.tzinfo is None:
            a0 = a0.tz_localize("UTC")
        if a1.tzinfo is None:
            a1 = a1.tz_localize("UTC")
        ts = pd.to_datetime(states_df["decision_time"], utc=True)
        states_df = states_df.loc[(ts >= a0) & (ts <= a1)].copy()

    state_list = states_df["research_state"].astype(str).tolist()
    state_counts = summarize_state_counts(state_list)
    transitions = summarize_transitions(state_list)
    components = summarize_components(states_df)
    rates = transition_rates(state_list)
    di_delay = mean_di_to_confirmed_delay(states_df)

    asof_text = build_asof_pine_script(cfg=cfg, symbol=symbol, timeframe=timeframe)
    retro_markers = build_retro_markers(
        di_ema_sequences=di_ema_sequences or [],
        outcomes=outcomes or [],
    )
    outcome_text = build_outcome_audit_pine_script(
        cfg=cfg,
        symbol=symbol,
        timeframe=timeframe,
        retro_markers=retro_markers,
    )

    asof_path = output_dir / ASOF_PINE_NAME
    outcome_path = output_dir / OUTCOME_PINE_NAME
    asof_path.write_text(asof_text, encoding="utf-8")
    outcome_path.write_text(outcome_text, encoding="utf-8")

    pd.DataFrame(state_counts).to_csv(output_dir / "trend_detector_state_counts.csv", index=False)
    pd.DataFrame(transitions).to_csv(output_dir / "trend_detector_transitions.csv", index=False)
    pd.DataFrame(components).to_csv(output_dir / "trend_detector_component_summary.csv", index=False)

    meta = {
        "asof_pine": str(asof_path),
        "outcome_pine": str(outcome_path),
        "asof_sha256": hashlib.sha256(asof_text.encode("utf-8")).hexdigest(),
        "outcome_sha256": hashlib.sha256(outcome_text.encode("utf-8")).hexdigest(),
        "n_bars": len(states_df),
        "state_counts": {r["state"]: r["n_bars"] for r in state_counts},
        "transition_rates": rates,
        "mean_di_to_confirmed_delay_bars": di_delay,
        "n_retro_markers": len(retro_markers),
        "asof_no_future_lookahead": True,
        "retro_does_not_affect_state": True,
        "thresholds": cfg.to_dict(),
    }
    return meta
