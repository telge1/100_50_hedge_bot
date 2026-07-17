"""Phase C3.4D — additive EMA9/20/59/200 context (research-only).

Architecture
------------
* C3.4B Protected Structure remains the sole structural ``major_direction`` truth.
* This module computes a parallel EMA context and never mutates C3.4B columns.
* EMA9/20 are micro/entry context only — they never flip structure major.
* EMA59/200 form a simple regime/guard context, not a structure replacement.
* Full EMA stack is descriptive only; it does **not** gate ``ema_regime_direction``.

Canonical math
--------------
* EMA: ``pandas.Series.ewm(span=period, adjust=False).mean()`` via ``indicators.ema``.
* ATR: Wilder ATR from ``indicators.atr_wilder`` / ``compute_indicator_frame``.
* Slope (C3.2A): ``(ema[t] - ema[t-3]) / atr[t]``.
* Band expand/compress (Clean-Regime defaults, fixed, not optimized):
  ``band_abs_atr`` change over 3 bars vs ``band_expand_min_change_atr=0.10``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.indicators import atr_wilder, compute_indicator_frame, ema
from research.regime_scanner.trend_detector_clean_regime import CleanRegimeConfig

BEARISH = -1
NEUTRAL = 0
BULLISH = 1

EMA_PERIODS: tuple[int, ...] = (9, 20, 59, 200)
SLOPE_LOOKBACK = 3
BAND_LOOKBACK = 3

# Fixed descriptive thresholds (Clean-Regime defaults — not optimized).
_BAND_EXPAND_MIN_ATR = float(CleanRegimeConfig().band_expand_min_change_atr)  # 0.10
_SLOPE_FLAT_MAX_ATR = float(CleanRegimeConfig().ema_flat_slope_max_atr)  # 0.10

STACK_STATES: tuple[str, ...] = (
    "bullish_full",
    "bearish_full",
    "bullish_partial",
    "bearish_partial",
    "mixed",
    "not_ready",
)

SLOPE_STATES: tuple[str, ...] = (
    "bullish_aligned",
    "bearish_aligned",
    "mixed",
    "flat",
    "not_ready",
)

BAND_STATES: tuple[str, ...] = (
    "expanding_bullish",
    "expanding_bearish",
    "compressing",
    "stable",
    "not_ready",
)

STRUCTURE_EMA_RELATIONS: tuple[str, ...] = (
    "aligned_bullish",
    "aligned_bearish",
    "structure_bullish_ema_neutral",
    "structure_bearish_ema_neutral",
    "structure_bullish_ema_bearish",
    "structure_bearish_ema_bullish",
    "structure_neutral_ema_bullish",
    "structure_neutral_ema_bearish",
    "both_neutral",
)

STRUCTURE_EMA_RELATION_CODE: dict[str, int] = {
    "aligned_bullish": 1,
    "aligned_bearish": -1,
    "structure_bullish_ema_neutral": 2,
    "structure_bearish_ema_neutral": -2,
    "structure_bullish_ema_bearish": 3,
    "structure_bearish_ema_bullish": -3,
    "structure_neutral_ema_bullish": 4,
    "structure_neutral_ema_bearish": -4,
    "both_neutral": 0,
}

# Actual C3.4B column names that must remain byte-identical after attach.
STRUCTURE_IMMUTABLE_COLS: tuple[str, ...] = (
    "major_direction",
    "protected_high",
    "protected_low",
    "candidate_protected_high",
    "candidate_protected_low",
    "candidate_leg",
    "external_bos_up",
    "external_bos_down",
    "external_bos_side",
    "choch_side",
    "protected_structure_state",
    "micro_swing_high",
    "micro_swing_low",
    "structure_age_bars",
)


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    out = a.astype("float64") / b.replace(0.0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan)


def _finite_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("float64").replace([np.inf, -np.inf], np.nan)


def _cmp_direction(fast: float, slow: float, *, ready: bool) -> int:
    if not ready or not np.isfinite(fast) or not np.isfinite(slow):
        return NEUTRAL
    if fast > slow:
        return BULLISH
    if fast < slow:
        return BEARISH
    return NEUTRAL


def classify_ema_stack_state(
    ema9: float,
    ema20: float,
    ema59: float,
    ema200: float,
    *,
    ready: bool,
) -> str:
    if not ready or not all(np.isfinite(v) for v in (ema9, ema20, ema59, ema200)):
        return "not_ready"
    full_bull = ema9 > ema20 > ema59 > ema200
    full_bear = ema9 < ema20 < ema59 < ema200
    if full_bull:
        return "bullish_full"
    if full_bear:
        return "bearish_full"
    micro_bull = ema9 > ema20
    micro_bear = ema9 < ema20
    regime_bull = ema59 > ema200
    regime_bear = ema59 < ema200
    if micro_bull and regime_bull:
        return "bullish_partial"
    if micro_bear and regime_bear:
        return "bearish_partial"
    return "mixed"


def classify_ema_slope_state(
    s9: float,
    s20: float,
    s59: float,
    s200: float,
    *,
    ready: bool,
    flat_max: float = _SLOPE_FLAT_MAX_ATR,
) -> str:
    vals = (s9, s20, s59, s200)
    if not ready or not all(np.isfinite(v) for v in vals):
        return "not_ready"
    if all(abs(v) <= flat_max for v in vals):
        return "flat"
    if all(v > flat_max for v in vals):
        return "bullish_aligned"
    if all(v < -flat_max for v in vals):
        return "bearish_aligned"
    return "mixed"


def classify_ema_band_state(
    *,
    spread_atr: float,
    spread_change_atr: float,
    micro_direction: int,
    ready: bool,
    expand_min: float = _BAND_EXPAND_MIN_ATR,
) -> str:
    if not ready or not np.isfinite(spread_atr) or not np.isfinite(spread_change_atr):
        return "not_ready"
    if spread_change_atr >= expand_min:
        if micro_direction > 0:
            return "expanding_bullish"
        if micro_direction < 0:
            return "expanding_bearish"
        return "stable"
    if spread_change_atr <= -expand_min:
        return "compressing"
    return "stable"


def classify_structure_ema_relation(structure_major: int, ema_regime: int) -> str:
    s = int(structure_major)
    e = int(ema_regime)
    if s > 0 and e > 0:
        return "aligned_bullish"
    if s < 0 and e < 0:
        return "aligned_bearish"
    if s > 0 and e == 0:
        return "structure_bullish_ema_neutral"
    if s < 0 and e == 0:
        return "structure_bearish_ema_neutral"
    if s > 0 and e < 0:
        return "structure_bullish_ema_bearish"
    if s < 0 and e > 0:
        return "structure_bearish_ema_bullish"
    if s == 0 and e > 0:
        return "structure_neutral_ema_bullish"
    if s == 0 and e < 0:
        return "structure_neutral_ema_bearish"
    return "both_neutral"


def cross_event(prev_fast: float, prev_slow: float, cur_fast: float, cur_slow: float) -> int:
    """+1 bullish cross, -1 bearish cross, 0 none. Requires finite values."""
    if not all(np.isfinite(v) for v in (prev_fast, prev_slow, cur_fast, cur_slow)):
        return 0
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        return BULLISH
    if prev_fast >= prev_slow and cur_fast < cur_slow:
        return BEARISH
    return NEUTRAL


def _age_since_change(values: Sequence[int]) -> np.ndarray:
    n = len(values)
    age = np.zeros(n, dtype=np.int64)
    last = 0
    for i in range(n):
        if i == 0:
            age[i] = 0
            last = 0
            continue
        if int(values[i]) != int(values[i - 1]):
            last = i
        age[i] = i - last
    return age


def compute_c3_4d_ema_context(
    ohlcv: pd.DataFrame,
    *,
    atr_period: int = 14,
    slope_lookback: int = SLOPE_LOOKBACK,
    band_lookback: int = BAND_LOOKBACK,
    reuse_indicator_frame: bool = True,
) -> pd.DataFrame:
    """Compute additive EMA context columns on closed OHLCV bars.

    Does not call C3.4B and does not write structure columns.
    """
    if ohlcv is None or ohlcv.empty:
        return pd.DataFrame()

    base = ohlcv.reset_index(drop=True).copy()
    missing = [c for c in ("open", "high", "low", "close") if c not in base.columns]
    if missing:
        raise ValueError(f"ohlcv missing columns: {missing}")

    close = _finite_series(base["close"])
    high = _finite_series(base["high"])
    low = _finite_series(base["low"])
    n = len(base)
    idx = np.arange(n)

    if reuse_indicator_frame:
        ind = compute_indicator_frame(base)
        for p in EMA_PERIODS:
            col = f"ema_{p}"
            if col not in ind.columns:
                ind[col] = ema(close, p)
            base[col] = _finite_series(ind[col])
        if "atr" in ind.columns:
            atr = _finite_series(ind["atr"])
        else:
            atr = atr_wilder(high, low, close, atr_period)
    else:
        for p in EMA_PERIODS:
            base[f"ema_{p}"] = ema(close, p)
        atr = atr_wilder(high, low, close, atr_period)

    base["atr_14"] = atr
    base["atr"] = atr

    # Canonical short aliases + ready flags (index+1 >= period, finite EMA).
    base["ema9"] = base["ema_9"]
    base["ema20"] = base["ema_20"]
    base["ema59"] = base["ema_59"]
    base["ema200"] = base["ema_200"]
    base["ema9_ready"] = ((idx + 1 >= 9) & base["ema_9"].notna()).astype(bool)
    base["ema20_ready"] = ((idx + 1 >= 20) & base["ema_20"].notna()).astype(bool)
    base["ema59_ready"] = ((idx + 1 >= 59) & base["ema_59"].notna()).astype(bool)
    base["ema200_ready"] = ((idx + 1 >= 200) & base["ema_200"].notna()).astype(bool)
    atr_ready = atr.notna() & (atr > 0)
    base["ema_context_ready"] = (base["ema200_ready"] & atr_ready).astype(bool)

    micro_ready = base["ema9_ready"] & base["ema20_ready"]
    mid_ready = base["ema20_ready"] & base["ema59_ready"]
    regime_ready = base["ema59_ready"] & base["ema200_ready"]

    e9 = base["ema9"].to_numpy(dtype=float)
    e20 = base["ema20"].to_numpy(dtype=float)
    e59 = base["ema59"].to_numpy(dtype=float)
    e200 = base["ema200"].to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    atr_a = atr.to_numpy(dtype=float)

    micro = np.zeros(n, dtype=np.int64)
    mid = np.zeros(n, dtype=np.int64)
    regime = np.zeros(n, dtype=np.int64)
    stack = np.array(["not_ready"] * n, dtype=object)

    for i in range(n):
        micro[i] = _cmp_direction(e9[i], e20[i], ready=bool(micro_ready.iloc[i]))
        mid[i] = _cmp_direction(e20[i], e59[i], ready=bool(mid_ready.iloc[i]))
        if not bool(regime_ready.iloc[i]) or not np.isfinite(e59[i]) or not np.isfinite(e200[i]) or not np.isfinite(c[i]):
            regime[i] = NEUTRAL
        elif e59[i] > e200[i] and c[i] > e200[i]:
            regime[i] = BULLISH
        elif e59[i] < e200[i] and c[i] < e200[i]:
            regime[i] = BEARISH
        else:
            regime[i] = NEUTRAL
        stack[i] = classify_ema_stack_state(
            e9[i], e20[i], e59[i], e200[i], ready=bool(base["ema_context_ready"].iloc[i])
        )

    base["ema_micro_direction"] = micro
    base["ema_mid_direction"] = mid
    base["ema_regime_direction"] = regime
    base["ema_stack_state"] = stack

    # Spreads / slopes / price distances (ATR-normalized)
    for left, right in ((9, 20), (20, 59), (59, 200)):
        spread = base[f"ema_{left}"] - base[f"ema_{right}"]
        base[f"ema{left}_{right}_spread_atr"] = _safe_div(spread, atr)

    for p in EMA_PERIODS:
        slope = base[f"ema_{p}"] - base[f"ema_{p}"].shift(int(slope_lookback))
        base[f"ema{p}_slope_atr"] = _safe_div(slope, atr)
        base[f"price_vs_ema{p}_atr"] = _safe_div(close - base[f"ema_{p}"], atr)

    # Band abs ATR change (9/20) — Clean-Regime style
    abs_spread_atr = base["ema9_20_spread_atr"].abs()
    band_change = abs_spread_atr - abs_spread_atr.shift(int(band_lookback))
    base["ema9_20_band_abs_atr"] = abs_spread_atr
    base["ema9_20_band_change_atr"] = band_change

    slope_state = np.array(["not_ready"] * n, dtype=object)
    band_state = np.array(["not_ready"] * n, dtype=object)
    s9 = base["ema9_slope_atr"].to_numpy(dtype=float)
    s20 = base["ema20_slope_atr"].to_numpy(dtype=float)
    s59 = base["ema59_slope_atr"].to_numpy(dtype=float)
    s200 = base["ema200_slope_atr"].to_numpy(dtype=float)
    bc = band_change.to_numpy(dtype=float)
    sa = abs_spread_atr.to_numpy(dtype=float)

    for i in range(n):
        slope_ready = bool(base["ema_context_ready"].iloc[i]) and i >= int(slope_lookback)
        slope_state[i] = classify_ema_slope_state(
            s9[i], s20[i], s59[i], s200[i], ready=slope_ready
        )
        band_ready = bool(micro_ready.iloc[i]) and bool(atr_ready.iloc[i]) and i >= int(band_lookback)
        band_state[i] = classify_ema_band_state(
            spread_atr=float(sa[i]) if np.isfinite(sa[i]) else float("nan"),
            spread_change_atr=float(bc[i]) if np.isfinite(bc[i]) else float("nan"),
            micro_direction=int(micro[i]),
            ready=band_ready,
        )

    base["ema_slope_state"] = slope_state
    base["ema_band_state"] = band_state

    # Cross events (closed-bar only, causal)
    cross_920 = np.zeros(n, dtype=np.int64)
    cross_2059 = np.zeros(n, dtype=np.int64)
    cross_59200 = np.zeros(n, dtype=np.int64)
    for i in range(1, n):
        if bool(micro_ready.iloc[i]) and bool(micro_ready.iloc[i - 1]):
            cross_920[i] = cross_event(e9[i - 1], e20[i - 1], e9[i], e20[i])
        if bool(mid_ready.iloc[i]) and bool(mid_ready.iloc[i - 1]):
            cross_2059[i] = cross_event(e20[i - 1], e59[i - 1], e20[i], e59[i])
        if bool(regime_ready.iloc[i]) and bool(regime_ready.iloc[i - 1]):
            cross_59200[i] = cross_event(e59[i - 1], e200[i - 1], e59[i], e200[i])

    base["ema9_20_cross_event"] = cross_920
    base["ema20_59_cross_event"] = cross_2059
    base["ema59_200_cross_event"] = cross_59200

    base["ema_micro_age_bars"] = _age_since_change(list(micro))
    base["ema_regime_age_bars"] = _age_since_change(list(regime))
    base["ema_mid_age_bars"] = _age_since_change(list(mid))

    # Sanitize
    numeric_cols = base.select_dtypes(include=[np.number]).columns
    base[numeric_cols] = base[numeric_cols].replace([np.inf, -np.inf], np.nan)
    return base


def structure_columns_hash(frame: pd.DataFrame, cols: Sequence[str] | None = None) -> str:
    """Stable hash of immutable C3.4B columns (for regression)."""
    use = [c for c in (cols or STRUCTURE_IMMUTABLE_COLS) if c in frame.columns]
    if not use:
        return hashlib.sha256(b"no_structure_cols").hexdigest()
    sub = frame.loc[:, list(use)].copy()
    for c in sub.columns:
        if pd.api.types.is_datetime64_any_dtype(sub[c]):
            sub[c] = pd.to_datetime(sub[c], utc=True).astype(str)
    blob = sub.to_csv(index=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def attach_structure_ema_relation(
    structure_frame: pd.DataFrame,
    ema_context_frame: pd.DataFrame | None = None,
    *,
    structure_major_col: str = "major_direction",
    ema_regime_col: str = "ema_regime_direction",
) -> pd.DataFrame:
    """Attach EMA context + ``structure_ema_relation`` without mutating structure values.

    If ``ema_context_frame`` is None, assumes EMA columns already live on
    ``structure_frame`` (same index / length).
    """
    if structure_frame is None or structure_frame.empty:
        return pd.DataFrame()

    before = structure_columns_hash(structure_frame)
    out = structure_frame.reset_index(drop=True).copy()

    if ema_context_frame is not None:
        ema = ema_context_frame.reset_index(drop=True)
        if len(ema) != len(out):
            raise ValueError(
                f"structure/ema length mismatch: {len(out)} vs {len(ema)}"
            )
        # Prefer explicit EMA context columns; never overwrite structure immutable cols.
        skip = set(STRUCTURE_IMMUTABLE_COLS) | {structure_major_col}
        for c in ema.columns:
            if c in skip:
                continue
            if c in ("open", "high", "low", "close", "volume", "timestamp", "bar_index"):
                # keep structure OHLCV; only fill if missing
                if c not in out.columns:
                    out[c] = ema[c].values
                continue
            out[c] = ema[c].values

    if structure_major_col not in out.columns:
        raise ValueError(f"missing {structure_major_col}")
    if ema_regime_col not in out.columns:
        raise ValueError(f"missing {ema_regime_col} — compute EMA context first")

    maj = pd.to_numeric(out[structure_major_col], errors="coerce").fillna(0).astype(int)
    reg = pd.to_numeric(out[ema_regime_col], errors="coerce").fillna(0).astype(int)
    rel = [classify_structure_ema_relation(int(s), int(e)) for s, e in zip(maj, reg)]
    out["structure_ema_relation"] = rel
    out["structure_ema_relation_code"] = [STRUCTURE_EMA_RELATION_CODE[r] for r in rel]
    out["structure_major_direction"] = maj  # explicit alias; same values

    after = structure_columns_hash(out)
    if before != after:
        raise RuntimeError(
            "C3.4D attach mutated immutable C3.4B structure columns "
            f"(before={before[:12]} after={after[:12]})"
        )
    out["c34b_structure_hash"] = before
    return out


def guard_block_long(
    structure_major: int,
    ema_regime: int,
    guard: str,
) -> bool:
    """Descriptive Long-block predicates (no production activation)."""
    s = int(structure_major)
    e = int(ema_regime)
    if guard == "G0":
        return False
    if guard == "G1":
        # block_long = structure_major == BEARISH
        return s == BEARISH
    if guard == "G1b":
        # block_long = structure_major == BEARISH AND ema_regime == BEARISH
        return s == BEARISH and e == BEARISH
    if guard == "G1c":
        # block_long = structure_major == BEARISH OR ema_regime == BEARISH
        return s == BEARISH or e == BEARISH
    raise ValueError(f"unknown guard: {guard}")


def guard_block_short(
    structure_major: int,
    ema_regime: int,
    guard: str,
) -> bool:
    """Mirrored Short-block predicates."""
    s = int(structure_major)
    e = int(ema_regime)
    if guard == "G0":
        return False
    if guard == "G1":
        return s == BULLISH
    if guard == "G1b":
        return s == BULLISH and e == BULLISH
    if guard == "G1c":
        return s == BULLISH or e == BULLISH
    raise ValueError(f"unknown guard: {guard}")


def guard_decision(side: str, structure_major: int, ema_regime: int, guard: str) -> str:
    """Return 'allow' or 'block' for a fill side."""
    side_l = str(side).lower()
    if side_l in {"long", "buy", "1"}:
        return "block" if guard_block_long(structure_major, ema_regime, guard) else "allow"
    if side_l in {"short", "sell", "-1"}:
        return "block" if guard_block_short(structure_major, ema_regime, guard) else "allow"
    raise ValueError(f"unknown side: {side}")


GUARD_FORMULAS: dict[str, dict[str, str]] = {
    "G0": {
        "name": "G0_no_htf_guard",
        "block_long": "False",
        "block_short": "False",
    },
    "G1": {
        "name": "G1_structure_only",
        "block_long": "structure_major_direction == BEARISH",
        "block_short": "structure_major_direction == BULLISH",
    },
    "G1b": {
        "name": "G1b_structure_AND_ema_regime_confirm",
        "block_long": "structure_major_direction == BEARISH AND ema_regime_direction == BEARISH",
        "block_short": "structure_major_direction == BULLISH AND ema_regime_direction == BULLISH",
    },
    "G1c": {
        "name": "G1c_structure_OR_ema_regime",
        "block_long": "structure_major_direction == BEARISH OR ema_regime_direction == BEARISH",
        "block_short": "structure_major_direction == BULLISH OR ema_regime_direction == BULLISH",
    },
}


def lookup_closed_htf_row(
    htf: pd.DataFrame,
    *,
    trigger_decision: pd.Timestamp,
    close_decision_col: str = "htf_close_decision",
) -> dict[str, Any]:
    """Last fully closed HTF bar with close_decision <= trigger_decision (no lookahead)."""
    if htf.empty or close_decision_col not in htf.columns:
        return {"found": False, "context_is_causal": True}
    close_dec = pd.to_datetime(htf[close_decision_col], utc=True)
    td = pd.Timestamp(trigger_decision)
    if td.tzinfo is None:
        td = td.tz_localize("UTC")
    else:
        td = td.tz_convert("UTC")
    mask = close_dec <= td
    if not mask.any():
        return {"found": False, "context_is_causal": True}
    idx = int(np.where(mask.to_numpy())[0][-1])
    row = htf.iloc[idx]
    assert pd.Timestamp(row[close_decision_col]) <= td
    return {
        "found": True,
        "context_is_causal": True,
        "row_index": idx,
        "row": row,
        "selected_bar_time": pd.Timestamp(row["timestamp"]) if "timestamp" in htf.columns else None,
        "selected_bar_close_time": pd.Timestamp(row[close_decision_col]),
    }


def semantics_doc() -> dict[str, Any]:
    return {
        "ema_periods": list(EMA_PERIODS),
        "slope_lookback": SLOPE_LOOKBACK,
        "band_lookback": BAND_LOOKBACK,
        "band_expand_min_change_atr": _BAND_EXPAND_MIN_ATR,
        "slope_flat_max_atr": _SLOPE_FLAT_MAX_ATR,
        "ema_micro_direction": "bullish if ema9>ema20; bearish if ema9<ema20; else neutral/not ready",
        "ema_mid_direction": "bullish if ema20>ema59; bearish if ema20<ema59; else neutral/not ready",
        "ema_regime_direction": (
            "bullish if ema59>ema200 AND close>ema200; "
            "bearish if ema59<ema200 AND close<ema200; else neutral"
        ),
        "ema_context_ready": "True iff ema200_ready and atr_14 finite > 0",
        "stack_does_not_gate_regime": True,
        "ema_never_mutates_c34b_major": True,
        "structure_ema_relations": list(STRUCTURE_EMA_RELATIONS),
        "structure_ema_relation_codes": dict(STRUCTURE_EMA_RELATION_CODE),
        "guards": GUARD_FORMULAS,
    }
