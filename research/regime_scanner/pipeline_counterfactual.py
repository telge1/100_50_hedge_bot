"""Research-only pipeline counterfactual: Setup/PA/Momentum + B3 + R2.

C0 baseline from existing pipeline CSVs; C1–C5 overlay precomputed B3 / R2
timelines with adaptive 2–3 candle confirmation. Disabled by default.
Does not mutate live strategy or pipeline artifacts. Outcomes attach after
entry decisions only — never feed back into state.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Mapping, Sequence

import pandas as pd

from research.regime_scanner.risk_off import would_block_long, would_block_short

CounterfactualVariant = Literal["C0", "C1", "C2", "C3", "C4", "C5"]
LifecycleState = Literal[
    "SETUP_SEEN",
    "BLOCKED_AT_SETUP",
    "WAITING_FOR_PA",
    "NO_PA_CONFIRMATION",
    "PA_CONFIRMED",
    "ABORTED_AT_PA",
    "WAITING_CONFIRMATION_1",
    "WAITING_CONFIRMATION_2",
    "WAITING_CONFIRMATION_3",
    "ABORTED_DURING_CONFIRMATION",
    "ENTRY_ALLOWED_AFTER_2",
    "ENTRY_ALLOWED_AFTER_3",
    "EXPIRED",
    "INVALIDATED",
]
EntryQuality = Literal["good", "weak", "mixed", "unknown"]
MomentumQuality = Literal["improving", "stable", "weakening", "invalid"]

ABORT_REASONS = {
    "B3_STRONG_BEARISH_AT_SETUP": "B3_STRONG_BEARISH_AT_SETUP",
    "B3_STRONG_BULLISH_AT_SETUP": "B3_STRONG_BULLISH_AT_SETUP",
    "R2_LONG_RISK_OFF_AT_SETUP": "R2_LONG_RISK_OFF_AT_SETUP",
    "R2_SHORT_RISK_OFF_AT_SETUP": "R2_SHORT_RISK_OFF_AT_SETUP",
    "B3_STRONG_BEARISH_AT_PA": "B3_STRONG_BEARISH_AT_PA",
    "B3_STRONG_BULLISH_AT_PA": "B3_STRONG_BULLISH_AT_PA",
    "R2_LONG_RISK_OFF_AT_PA": "R2_LONG_RISK_OFF_AT_PA",
    "R2_SHORT_RISK_OFF_AT_PA": "R2_SHORT_RISK_OFF_AT_PA",
    "B3_STRONG_BEARISH_DURING_CONFIRMATION": "B3_STRONG_BEARISH_DURING_CONFIRMATION",
    "B3_STRONG_BULLISH_DURING_CONFIRMATION": "B3_STRONG_BULLISH_DURING_CONFIRMATION",
    "R2_LONG_RISK_OFF_DURING_CONFIRMATION": "R2_LONG_RISK_OFF_DURING_CONFIRMATION",
    "R2_SHORT_RISK_OFF_DURING_CONFIRMATION": "R2_SHORT_RISK_OFF_DURING_CONFIRMATION",
    "MOMENTUM_INVALIDATED": "MOMENTUM_INVALIDATED",
    "RISK_ELEVATED_NOT_CLEARED": "RISK_ELEVATED_NOT_CLEARED",
    "PA_STRUCTURE_INVALIDATED": "PA_STRUCTURE_INVALIDATED",
    "PA_INVALID_AT_CONFIRMATION_START": "PA_INVALID_AT_CONFIRMATION_START",
    "C5_SCORE_DROP_NOT_MET": "C5_SCORE_DROP_NOT_MET",
    "C5_MOMENTUM_NOT_STRONG": "C5_MOMENTUM_NOT_STRONG",
}

_TERMINAL: frozenset[str] = frozenset(
    {
        "BLOCKED_AT_SETUP",
        "NO_PA_CONFIRMATION",
        "ABORTED_AT_PA",
        "ABORTED_DURING_CONFIRMATION",
        "ENTRY_ALLOWED_AFTER_2",
        "ENTRY_ALLOWED_AFTER_3",
        "EXPIRED",
        "INVALIDATED",
    }
)
_WAIT = {1: "WAITING_CONFIRMATION_1", 2: "WAITING_CONFIRMATION_2", 3: "WAITING_CONFIRMATION_3"}
_VARIANT_FLAGS: dict[str, tuple[bool, bool]] = {
    "C0": (False, False),
    "C1": (True, False),
    "C2": (False, True),
    "C3": (True, True),
    "C4": (True, True),
    "C5": (True, True),
}


@dataclass(frozen=True)
class PipelineCounterfactualConfig:
    enabled: bool = False
    variant: CounterfactualVariant = "C0"
    use_b3: bool = False
    use_r2: bool = False
    confirm_candles_normal: int = 2
    confirm_candles_elevated: int = 3
    c5_min_score_drop: float = 1.0
    c5_require_strong_momentum: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def variant_config(v: CounterfactualVariant | str) -> PipelineCounterfactualConfig:
    key = str(v).upper()
    if key not in _VARIANT_FLAGS:
        raise ValueError(f"unknown counterfactual variant: {v!r}")
    use_b3, use_r2 = _VARIANT_FLAGS[key]
    return PipelineCounterfactualConfig(variant=key, use_b3=use_b3, use_r2=use_r2)  # type: ignore[arg-type]


def default_pipeline_counterfactual_config(
    *, variant: CounterfactualVariant = "C0"
) -> PipelineCounterfactualConfig:
    return replace(variant_config(variant), enabled=False)


def is_terminal(state: object) -> bool:
    return str(state or "") in _TERMINAL


def _to_utc(ts: object) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _finite(v: object) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _side(side: object) -> str:
    return str(side or "").strip().lower()


def lookup_asof(
    df: pd.DataFrame | None,
    ts: object,
    time_col: str = "decision_time",
) -> dict[str, Any]:
    """Last row with ``time_col <= ts`` (merge_asof backward)."""
    if df is None or len(df) == 0 or ts is None:
        return {}
    try:
        t = _to_utc(ts)
    except (TypeError, ValueError):
        return {}
    frame = df
    col = time_col
    if col not in frame.columns:
        if col == "decision_time" and "bar_close_time" in frame.columns:
            col = "bar_close_time"
        else:
            return {}
    work = frame[[c for c in frame.columns]].copy()
    work[col] = pd.to_datetime(work[col], utc=True)
    work = work.sort_values(col)
    merged = pd.merge_asof(pd.DataFrame({col: [t]}), work, on=col, direction="backward")
    if merged.empty or pd.isna(merged.iloc[0].get(col)):
        return {}
    return {k: (None if pd.isna(v) else v) for k, v in merged.iloc[0].to_dict().items()}


def confirm_times_after(
    decision_index: pd.DatetimeIndex | Sequence[object],
    pa_ts: object,
    n: int = 3,
) -> list[pd.Timestamp | None]:
    """First ``n`` decision times strictly after ``pa_ts``."""
    after = _to_utc(pa_ts)
    later = [_to_utc(t) for t in decision_index if _to_utc(t) > after]
    return [later[i] if i < len(later) else None for i in range(n)]


def _b3(row: Mapping[str, Any]) -> str:
    for k in ("direction_gate_state", "b3_state"):
        if row.get(k) is not None:
            return str(row[k]).strip().lower()
    return ""


def _rs(row: Mapping[str, Any]) -> str:
    return str(row.get("risk_state") or "").strip().lower()


def _score(row: Mapping[str, Any], side: str) -> float | None:
    keys = (
        ("long_risk_score", "risk_score_long")
        if _side(side) == "long"
        else ("short_risk_score", "risk_score_short")
    )
    for k in keys:
        v = _finite(row.get(k))
        if v is not None:
            return v
    return None


def _elevated(risk_state: object, side: str) -> bool:
    s = str(risk_state or "")
    return s == ("long_risk_elevated" if _side(side) == "long" else "short_risk_elevated")


def _risk_off(risk_state: object, side: str) -> bool:
    s = str(risk_state or "")
    if _side(side) == "long":
        return s in {"long_risk_off", "covered_by_strong_bearish"}
    return s in {"short_risk_off", "covered_by_strong_bullish"}


def _b3_opp(b3: object, side: str) -> bool:
    s = str(b3 or "").strip().lower()
    return s == ("strong_bearish" if _side(side) == "long" else "strong_bullish")


def _reason(kind: str, side: str, stage: str) -> str:
    """kind in {b3, r2}; stage in {setup, pa, confirm}."""
    long = _side(side) == "long"
    if kind == "b3":
        tag = "BEARISH" if long else "BULLISH"
        if stage == "setup":
            return ABORT_REASONS[f"B3_STRONG_{tag}_AT_SETUP"]
        if stage == "pa":
            return ABORT_REASONS[f"B3_STRONG_{tag}_AT_PA"]
        return ABORT_REASONS[f"B3_STRONG_{tag}_DURING_CONFIRMATION"]
    tag = "LONG" if long else "SHORT"
    if stage == "setup":
        return ABORT_REASONS[f"R2_{tag}_RISK_OFF_AT_SETUP"]
    if stage == "pa":
        return ABORT_REASONS[f"R2_{tag}_RISK_OFF_AT_PA"]
    return ABORT_REASONS[f"R2_{tag}_RISK_OFF_DURING_CONFIRMATION"]


def _gate_reasons(
    *,
    side: str,
    stage: str,
    r2_row: Mapping[str, Any],
    b3_row: Mapping[str, Any],
    use_b3: bool,
    use_r2: bool,
) -> list[str]:
    reasons: list[str] = []
    b3 = _b3(b3_row) or _b3(r2_row)
    rs = _rs(r2_row)
    if use_b3 and _b3_opp(b3, side):
        reasons.append(_reason("b3", side, stage))
    if use_r2 and _risk_off(rs, side):
        reasons.append(_reason("r2", side, stage))
    if not reasons and (use_b3 or use_r2):
        blocked = (
            would_block_long(rs, b3_state=b3)
            if _side(side) == "long"
            else would_block_short(rs, b3_state=b3)
        )
        if blocked:
            if use_b3 and _b3_opp(b3, side):
                reasons.append(_reason("b3", side, stage))
            elif use_r2:
                reasons.append(_reason("r2", side, stage))
    return reasons


def classify_entry_quality(
    mfe: float | None,
    mae: float | None,
    reached_025: bool | None,
    returned: bool | None = None,
) -> EntryQuality:
    """Fixed heuristic (set before inspecting variant results)."""
    del returned
    weak = reached_025 is False or (
        mae is not None and mae >= 1.5 and (mfe is None or mfe < 0.25)
    )
    good = reached_025 is True and (mae is None or mae < 1.0)
    if good and not weak:
        return "good"
    if weak and not good:
        return "weak"
    if good and weak:
        return "mixed"
    if reached_025 is None and mfe is None and mae is None:
        return "unknown"
    return "mixed"


def compute_forward_outcome(
    candles_5m: pd.DataFrame,
    entry_ts: object,
    entry_price: float,
    side: str,
    horizon_bars: int = 72,
) -> dict[str, Any]:
    """Causal forward from entry (excludes entry bar). 15/30/60m = 3/6/12 bars."""
    base: dict[str, Any] = {
        "evaluable": False,
        "entry_ts": str(entry_ts) if entry_ts is not None else None,
        "entry_price": entry_price,
        "side": _side(side),
        "horizon_bars": horizon_bars,
        "mfe_pct": None,
        "mae_pct": None,
        "deepest_adverse": None,
        "reached_plus_025": None,
        "minutes_to_025": None,
        "returned_to_entry": None,
        "minutes_to_return": None,
        "adverse_15m": None,
        "adverse_30m": None,
        "adverse_60m": None,
        "favorable_15m": None,
        "favorable_30m": None,
        "favorable_60m": None,
        "entry_quality": "unknown",
    }
    if candles_5m is None or len(candles_5m) == 0 or not entry_price:
        return base
    try:
        et = _to_utc(entry_ts)
    except (TypeError, ValueError):
        return base

    frame = candles_5m.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    dec = (
        pd.to_datetime(frame["decision_time"], utc=True)
        if "decision_time" in frame.columns
        else frame["timestamp"] + pd.Timedelta(minutes=5)
    )
    future = frame.loc[dec > et].head(int(horizon_bars))
    if future.empty:
        base["reason"] = "INSUFFICIENT_FUTURE_CANDLES"
        return base

    side_l = _side(side)
    mfe = mae = 0.0
    reached = False
    returned = False
    min_025: float | None = None
    min_ret: float | None = None
    snap: dict[int, tuple[float, float]] = {}

    for offset, (_, row) in enumerate(future.iterrows(), start=1):
        hi, lo, cl = _finite(row.get("high")), _finite(row.get("low")), _finite(row.get("close"))
        if hi is None or lo is None:
            continue
        if side_l == "long":
            fav = max(0.0, (hi - entry_price) / abs(entry_price) * 100.0)
            adv = max(0.0, (entry_price - lo) / abs(entry_price) * 100.0)
            dret = (cl - entry_price) / abs(entry_price) * 100.0 if cl is not None else None
        else:
            fav = max(0.0, (entry_price - lo) / abs(entry_price) * 100.0)
            adv = max(0.0, (hi - entry_price) / abs(entry_price) * 100.0)
            dret = (entry_price - cl) / abs(entry_price) * 100.0 if cl is not None else None
        mfe, mae = max(mfe, fav), max(mae, adv)
        mins = float(offset * 5)
        if not reached and fav >= 0.25:
            reached, min_025 = True, mins
        if not returned and dret is not None and dret >= 0.0 and offset > 1 and mae > 0:
            returned, min_ret = True, mins
        if offset in {3, 6, 12}:
            snap[offset] = (adv, fav)

    return {
        **base,
        "evaluable": True,
        "entry_ts": str(et),
        "entry_price": float(entry_price),
        "available_bars": int(len(future)),
        "mfe_pct": float(mfe),
        "mae_pct": float(mae),
        "deepest_adverse": float(mae),
        "reached_plus_025": bool(reached),
        "minutes_to_025": min_025,
        "returned_to_entry": bool(returned),
        "minutes_to_return": min_ret,
        "adverse_15m": snap.get(3, (None, None))[0],
        "adverse_30m": snap.get(6, (None, None))[0],
        "adverse_60m": snap.get(12, (None, None))[0],
        "favorable_15m": snap.get(3, (None, None))[1],
        "favorable_30m": snap.get(6, (None, None))[1],
        "favorable_60m": snap.get(12, (None, None))[1],
        "entry_quality": classify_entry_quality(mfe, mae, reached, returned),
    }


def momentum_candle_ok_and_quality(
    candle_row: Mapping[str, Any] | None,
    side: str,
    prev_quality_score: float | None = None,
    *,
    existing_confirmation: Mapping[str, Any] | None = None,
) -> tuple[bool, MomentumQuality, dict[str, Any]]:
    """Descriptive quality similar to risk_off.momentum_candle_quality.

    Labels: improving|stable|weakening|invalid. C0 may pass existing confirmation.
    """
    if existing_confirmation is not None:
        conf = str(existing_confirmation.get("confidence") or "").lower()
        quality: MomentumQuality = (
            "improving" if conf == "high" else "weakening" if conf == "low" else "stable"
        )
        metrics = {
            "source": "existing_confirmation",
            "confidence": existing_confirmation.get("confidence"),
            "body_to_range_ratio": existing_confirmation.get("body_to_range_ratio"),
            "close_location_ratio": existing_confirmation.get("close_location_ratio"),
            "range_atr_ratio": existing_confirmation.get("range_atr_ratio"),
            "directional_body": existing_confirmation.get("directional_body"),
            "quality_score": None,
            "quality": quality,
            "ok": True,
        }
        return True, quality, metrics

    if candle_row is None:
        return False, "invalid", {"source": "missing_candle", "ok": False, "quality": "invalid"}

    side_l = _side(side)
    o, h, lo, c = (_finite(candle_row.get(k)) for k in ("open", "high", "low", "close"))
    atr = _finite(candle_row.get("atr"))
    ema9, ema20 = _finite(candle_row.get("ema_9")), _finite(candle_row.get("ema_20"))

    body_ok_b = False
    close_loc = atr_ratio = None
    if None not in (o, h, lo, c):
        rng = h - lo  # type: ignore[operator]
        body = abs(c - o)  # type: ignore[operator]
        body_ok_b = bool(rng > 0 and (body / rng) >= 0.5)
        if rng > 0:
            close_loc = (c - lo) / rng if side_l == "long" else (h - c) / rng  # type: ignore[operator]
        if atr and atr > 0:
            atr_ratio = rng / atr

    bullish = bool(o is not None and c is not None and c > o)
    bearish = bool(o is not None and c is not None and c < o)
    directional = bullish if side_l == "long" else bearish
    adverse = bearish if side_l == "long" else bullish
    impulse = False
    if atr and atr > 0 and o is not None and c is not None:
        move = (o - c) if side_l == "long" else (c - o)
        impulse = move / atr >= 1.0
    if side_l == "long":
        ema_ok = bool((c and ema20 and c > ema20) or (ema9 and ema20 and ema9 > ema20))
    else:
        ema_ok = bool((c and ema20 and c < ema20) or (ema9 and ema20 and ema9 < ema20))

    score = 0
    score += 2 if directional and body_ok_b else 1 if directional else 0
    if close_loc is not None and close_loc >= 0.6:
        score += 1
    if atr_ratio is not None and 0.3 <= atr_ratio <= 3.0:
        score += 1
    if ema_ok:
        score += 1
    if adverse:
        score -= 2
    if impulse:
        score -= 2

    if score <= -2 or (adverse and impulse):
        quality = "invalid"
    elif score >= 3:
        quality = "improving"
    elif score <= 0:
        quality = "weakening"
    else:
        quality = "stable"

    if prev_quality_score is not None and quality != "invalid":
        if score > prev_quality_score + 0.5:
            quality = "improving"
        elif score < prev_quality_score - 0.5:
            quality = "weakening"

    ok = quality in {"improving", "stable"}
    return ok, quality, {
        "source": "candle_row",
        "quality_score": score,
        "quality": quality,
        "ok": ok,
        "body_ok": body_ok_b,
        "close_location_ratio": close_loc,
        "range_atr_ratio": atr_ratio,
        "directional": directional,
        "adverse": adverse,
        "impulse_adverse": impulse,
        "ema_ok": ema_ok,
        "open": o,
        "high": h,
        "low": lo,
        "close": c,
        "atr": atr,
        "ema_9": ema9,
        "ema_20": ema20,
        "ret_1": _finite(candle_row.get("ret_1")),
        "plus_di": _finite(candle_row.get("plus_di")),
        "minus_di": _finite(candle_row.get("minus_di")),
        "adx": _finite(candle_row.get("adx")),
        "ema9_slope": _finite(candle_row.get("ema9_slope") or candle_row.get("ema_9_slope_3_pct")),
        "ema20_slope": _finite(candle_row.get("ema20_slope") or candle_row.get("ema_20_slope_12_pct")),
    }


def _ensure_decision_col(candles: pd.DataFrame) -> pd.DataFrame:
    if "decision_time" in candles.columns:
        out = candles
    else:
        out = candles.copy()
        out["decision_time"] = pd.to_datetime(out["timestamp"], utc=True) + pd.Timedelta(minutes=5)
    return out


def _candle_at(candles_5m: pd.DataFrame | None, decision_ts: object) -> dict[str, Any] | None:
    if candles_5m is None or len(candles_5m) == 0 or decision_ts is None:
        return None
    return lookup_asof(_ensure_decision_col(candles_5m), decision_ts) or None


def _entry_price(candles_5m: pd.DataFrame | None, entry_ts: object) -> float | None:
    row = _candle_at(candles_5m, entry_ts)
    return _finite(row.get("close")) if row else None


def c0_reproduction_row(
    setup_id: str,
    baseline: Mapping[str, Any],
    counterfactual: Mapping[str, Any],
) -> dict[str, Any]:
    keys = (
        "setup_id",
        "side",
        "setup_activation_timestamp",
        "pa_structure_break_timestamp",
        "momentum_confirmation_timestamp",
        "final_state",
        "entry_allowed",
        "entry_timestamp",
    )
    mismatches: list[str] = []
    detail: dict[str, Any] = {}
    for k in keys:
        b = baseline.get(k) if k != "setup_id" else baseline.get("setup_id", setup_id)
        c = counterfactual.get(k) if k != "setup_id" else setup_id
        match = (None if b is None else str(b)) == (None if c is None else str(c))
        detail[f"baseline_{k}"] = b
        detail[f"cf_{k}"] = c
        detail[f"match_{k}"] = match
        if not match:
            mismatches.append(k)
    return {"setup_id": setup_id, "matches": not mismatches, "mismatch_fields": mismatches, **detail}


def _entry_at_required(
    *,
    cfg: PipelineCounterfactualConfig,
    side: str,
    i: int,
    required: int,
    ok: bool,
    quality: MomentumQuality,
    qualities: Sequence[MomentumQuality],
    r2_c: Mapping[str, Any],
    score_pa: float | None,
) -> tuple[str, LifecycleState, list[str]]:
    if i == 2 and required == 2:
        if ok:
            return "entry", "ENTRY_ALLOWED_AFTER_2", []
        return "abort", "ABORTED_DURING_CONFIRMATION", [ABORT_REASONS["MOMENTUM_INVALIDATED"]]

    if cfg.variant == "C4":
        if cfg.use_r2 and _rs(r2_c) != "normal":
            return "abort", "ABORTED_DURING_CONFIRMATION", [ABORT_REASONS["RISK_ELEVATED_NOT_CLEARED"]]
        if ok:
            return "entry", "ENTRY_ALLOWED_AFTER_3", []
        return "abort", "ABORTED_DURING_CONFIRMATION", [ABORT_REASONS["MOMENTUM_INVALIDATED"]]

    if cfg.variant == "C5":
        now = _score(r2_c, side)
        drop_ok = True
        if cfg.use_r2 and score_pa is not None:
            drop_ok = now is not None and (score_pa - now) >= float(cfg.c5_min_score_drop)
        strong = True
        if cfg.c5_require_strong_momentum:
            strong = quality == "improving" or (
                quality == "stable" and all(q != "invalid" for q in qualities)
            )
        if cfg.use_r2 and _risk_off(_rs(r2_c), side):
            return "abort", "ABORTED_DURING_CONFIRMATION", [_reason("r2", side, "confirm")]
        if not drop_ok:
            return "abort", "ABORTED_DURING_CONFIRMATION", [ABORT_REASONS["C5_SCORE_DROP_NOT_MET"]]
        if not strong:
            return "abort", "ABORTED_DURING_CONFIRMATION", [ABORT_REASONS["C5_MOMENTUM_NOT_STRONG"]]
        if ok:
            return "entry", "ENTRY_ALLOWED_AFTER_3", []
        return "abort", "ABORTED_DURING_CONFIRMATION", [ABORT_REASONS["MOMENTUM_INVALIDATED"]]

    # C1 / C2 / C3
    if ok:
        return "entry", ("ENTRY_ALLOWED_AFTER_2" if required <= 2 else "ENTRY_ALLOWED_AFTER_3"), []
    return "abort", "ABORTED_DURING_CONFIRMATION", [ABORT_REASONS["MOMENTUM_INVALIDATED"]]


def _pack(
    *,
    setup_id: str,
    side: str,
    setup_ts: pd.Timestamp,
    pa_row: Mapping[str, Any] | None,
    existing_mom_row: Mapping[str, Any] | None,
    cfg: PipelineCounterfactualConfig,
    final_state: str,
    abort_reasons: list[str],
    state_path: list[dict[str, Any]],
    confirm_records: list[dict[str, Any]],
    entry_ts: pd.Timestamp | None,
    entry_price: float | None,
    required: int,
    elevated_setup: bool,
    elevated_pa: bool,
    prevented: bool,
    score_pa: float | None,
    candles_5m: pd.DataFrame | None,
    r2_setup: dict[str, Any] | None = None,
    b3_setup: dict[str, Any] | None = None,
    r2_pa: dict[str, Any] | None = None,
    b3_pa: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pa_ts = None
    if pa_row and pa_row.get("structure_break_timestamp") is not None:
        pa_ts = str(_to_utc(pa_row["structure_break_timestamp"]))
    allowed = str(final_state).startswith("ENTRY_ALLOWED")
    outcome = None
    if allowed and entry_ts is not None and entry_price is not None:
        outcome = compute_forward_outcome(candles_5m, entry_ts, entry_price, side)
    mom_ts = None
    if allowed and entry_ts is not None:
        mom_ts = str(entry_ts)
    elif existing_mom_row:
        raw = existing_mom_row.get("confirmation_timestamp") or existing_mom_row.get(
            "confirming_candle_timestamp"
        )
        mom_ts = str(raw) if raw is not None else None
    return {
        "setup_id": setup_id,
        "side": side,
        "variant": cfg.variant,
        "config": cfg.to_dict(),
        "setup_activation_timestamp": str(setup_ts),
        "pa_structure_break_timestamp": pa_ts,
        "confirmation_level": (pa_row or {}).get("confirmation_level") if pa_row else None,
        "momentum_confirmation_timestamp": mom_ts,
        "final_state": final_state,
        "is_terminal": is_terminal(final_state),
        "abort_reasons": list(abort_reasons),
        "primary_abort_reason": abort_reasons[0] if abort_reasons else None,
        "entry_allowed": allowed,
        "entry_timestamp": str(entry_ts) if entry_ts is not None else None,
        "entry_price": entry_price,
        "required_confirm_candles": required,
        "elevated_at_setup": elevated_setup,
        "elevated_at_pa": elevated_pa,
        "prevented_by_gate": prevented,
        "risk_score_at_pa": score_pa,
        "risk_state_at_setup": _rs(r2_setup or {}),
        "b3_state_at_setup": _b3(b3_setup or {}) or _b3(r2_setup or {}),
        "risk_state_at_pa": _rs(r2_pa or {}) if r2_pa is not None else None,
        "b3_state_at_pa": (
            (_b3(b3_pa or {}) or _b3(r2_pa or {})) if (r2_pa is not None or b3_pa) else None
        ),
        "state_path": state_path,
        "confirmation_candles": confirm_records,
        "forward_outcome": outcome,
        "existing_momentum_present": existing_mom_row is not None,
    }


def simulate_sequence(
    *,
    setup_row: Mapping[str, Any],
    pa_row: Mapping[str, Any] | None,
    existing_mom_row: Mapping[str, Any] | None,
    r2_timeline: pd.DataFrame | None,
    b3_timeline: pd.DataFrame | None,
    candles_5m: pd.DataFrame | None,
    decision_index: pd.DatetimeIndex | Sequence[object],
    cfg: PipelineCounterfactualConfig | None = None,
) -> dict[str, Any]:
    """Walk one setup: Setup → (B3/R2) → PA → adaptive confirmation → entry."""
    cfg = cfg or PipelineCounterfactualConfig()
    setup_id = str(setup_row.get("setup_id") or "")
    side = _side(setup_row.get("side") or setup_row.get("setup_side"))
    setup_ts = _to_utc(setup_row["setup_activation_timestamp"])
    path: list[dict[str, Any]] = []
    confirms: list[dict[str, Any]] = []
    aborts: list[str] = []
    required = int(cfg.confirm_candles_normal)
    elev_s = elev_p = prevented = False
    score_pa: float | None = None
    entry_ts: pd.Timestamp | None = None
    entry_price: float | None = None
    r2_pa: dict[str, Any] = {}
    b3_pa: dict[str, Any] = {}

    def note(state: str, ts: object, **extra: Any) -> None:
        path.append({"state": state, "timestamp": str(ts) if ts is not None else None, **extra})

    note("SETUP_SEEN", setup_ts, stage="setup")
    r2_s = lookup_asof(r2_timeline, setup_ts) if cfg.use_r2 else {}
    b3_s = lookup_asof(b3_timeline, setup_ts) if cfg.use_b3 else {}
    elev_s = bool(cfg.use_r2 and _elevated(_rs(r2_s), side))

    blocks = _gate_reasons(
        side=side, stage="setup", r2_row=r2_s, b3_row=b3_s, use_b3=cfg.use_b3, use_r2=cfg.use_r2
    )
    if blocks:
        note("BLOCKED_AT_SETUP", setup_ts, stage="setup", reasons=list(blocks))
        return _pack(
            setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
            existing_mom_row=existing_mom_row, cfg=cfg, final_state="BLOCKED_AT_SETUP",
            abort_reasons=blocks, state_path=path, confirm_records=confirms,
            entry_ts=None, entry_price=None, required=required, elevated_setup=elev_s,
            elevated_pa=False, prevented=True, score_pa=None, candles_5m=candles_5m,
            r2_setup=r2_s, b3_setup=b3_s,
        )

    note("WAITING_FOR_PA", setup_ts, stage="setup")
    if pa_row is None or pa_row.get("structure_break_timestamp") is None:
        note("NO_PA_CONFIRMATION", setup_ts, stage="pa")
        return _pack(
            setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
            existing_mom_row=existing_mom_row, cfg=cfg, final_state="NO_PA_CONFIRMATION",
            abort_reasons=[], state_path=path, confirm_records=confirms,
            entry_ts=None, entry_price=None, required=required, elevated_setup=elev_s,
            elevated_pa=False, prevented=False, score_pa=None, candles_5m=candles_5m,
            r2_setup=r2_s, b3_setup=b3_s,
        )

    pa_ts = _to_utc(pa_row["structure_break_timestamp"])
    note("PA_CONFIRMED", pa_ts, stage="pa")
    r2_pa = lookup_asof(r2_timeline, pa_ts) if cfg.use_r2 else {}
    b3_pa = lookup_asof(b3_timeline, pa_ts) if cfg.use_b3 else {}
    elev_p = bool(cfg.use_r2 and _elevated(_rs(r2_pa), side))
    score_pa = _score(r2_pa, side) if cfg.use_r2 else None
    required = (
        int(cfg.confirm_candles_elevated)
        if (elev_s or elev_p)
        else int(cfg.confirm_candles_normal)
    )

    blocks = _gate_reasons(
        side=side, stage="pa", r2_row=r2_pa, b3_row=b3_pa, use_b3=cfg.use_b3, use_r2=cfg.use_r2
    )
    if blocks:
        note("ABORTED_AT_PA", pa_ts, stage="pa", reasons=list(blocks))
        return _pack(
            setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
            existing_mom_row=existing_mom_row, cfg=cfg, final_state="ABORTED_AT_PA",
            abort_reasons=blocks, state_path=path, confirm_records=confirms,
            entry_ts=None, entry_price=None, required=required, elevated_setup=elev_s,
            elevated_pa=elev_p, prevented=True, score_pa=score_pa, candles_5m=candles_5m,
            r2_setup=r2_s, b3_setup=b3_s, r2_pa=r2_pa, b3_pa=b3_pa,
        )

    # ----- C0: existing momentum CSV -----
    if cfg.variant == "C0":
        if existing_mom_row is None:
            term = str((pa_row or {}).get("momentum_terminal_event") or "").lower()
            st: LifecycleState = "INVALIDATED" if term == "invalidated" else "EXPIRED"
            note(st, pa_ts, stage="momentum", note="no_existing_momentum")
            return _pack(
                setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
                existing_mom_row=None, cfg=cfg, final_state=st, abort_reasons=[],
                state_path=path, confirm_records=confirms, entry_ts=None, entry_price=None,
                required=required, elevated_setup=elev_s, elevated_pa=elev_p, prevented=False,
                score_pa=score_pa, candles_5m=candles_5m, r2_setup=r2_s, b3_setup=b3_s,
                r2_pa=r2_pa, b3_pa=b3_pa,
            )
        raw_ts = existing_mom_row.get("confirmation_timestamp") or existing_mom_row.get(
            "confirming_candle_timestamp"
        )
        if raw_ts is None:
            note("EXPIRED", pa_ts, stage="momentum")
            return _pack(
                setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
                existing_mom_row=existing_mom_row, cfg=cfg, final_state="EXPIRED",
                abort_reasons=[], state_path=path, confirm_records=confirms,
                entry_ts=None, entry_price=None, required=required, elevated_setup=elev_s,
                elevated_pa=elev_p, prevented=False, score_pa=score_pa, candles_5m=candles_5m,
                r2_setup=r2_s, b3_setup=b3_s, r2_pa=r2_pa, b3_pa=b3_pa,
            )
        mom_ts = _to_utc(raw_ts)
        try:
            age = int(existing_mom_row.get("candles_after_price_action_confirmation") or 2)
        except (TypeError, ValueError):
            age = 2
        st = "ENTRY_ALLOWED_AFTER_3" if age >= 3 else "ENTRY_ALLOWED_AFTER_2"
        ok, quality, mq = momentum_candle_ok_and_quality(
            None, side, existing_confirmation=existing_mom_row
        )
        confirms.append(
            {
                "setup_id": setup_id,
                "variant": "C0",
                "candle_index": max(age, 1),
                "timestamp": str(mom_ts),
                "momentum_ok": ok,
                "momentum_quality": quality,
                "momentum_metrics": mq,
                "decision": "entry",
                "final_state_after": st,
                "source": "existing_momentum_csv",
            }
        )
        note(st, mom_ts, stage="momentum", candles_after=age)
        return _pack(
            setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
            existing_mom_row=existing_mom_row, cfg=cfg, final_state=st, abort_reasons=[],
            state_path=path, confirm_records=confirms, entry_ts=mom_ts,
            entry_price=_entry_price(candles_5m, mom_ts), required=required,
            elevated_setup=elev_s, elevated_pa=elev_p, prevented=False, score_pa=score_pa,
            candles_5m=candles_5m, r2_setup=r2_s, b3_setup=b3_s, r2_pa=r2_pa, b3_pa=b3_pa,
        )

    # ----- C1–C5: overlay B3/R2 on existing momentum path (no new momentum rules) -----
    # Reuse baseline momentum confirmation when present; without it match C0 → EXPIRED.
    if existing_mom_row is None:
        note("EXPIRED", pa_ts, stage="momentum", note="no_existing_momentum_same_as_c0")
        return _pack(
            setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
            existing_mom_row=None, cfg=cfg, final_state="EXPIRED", abort_reasons=[],
            state_path=path, confirm_records=confirms, entry_ts=None, entry_price=None,
            required=required, elevated_setup=elev_s, elevated_pa=elev_p, prevented=False,
            score_pa=score_pa, candles_5m=candles_5m, r2_setup=r2_s, b3_setup=b3_s,
            r2_pa=r2_pa, b3_pa=b3_pa,
        )

    raw_ts = existing_mom_row.get("confirmation_timestamp") or existing_mom_row.get(
        "confirming_candle_timestamp"
    )
    if raw_ts is None:
        note("EXPIRED", pa_ts, stage="momentum", note="missing_mom_timestamp")
        return _pack(
            setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
            existing_mom_row=existing_mom_row, cfg=cfg, final_state="EXPIRED",
            abort_reasons=[], state_path=path, confirm_records=confirms,
            entry_ts=None, entry_price=None, required=required, elevated_setup=elev_s,
            elevated_pa=elev_p, prevented=False, score_pa=score_pa, candles_5m=candles_5m,
            r2_setup=r2_s, b3_setup=b3_s, r2_pa=r2_pa, b3_pa=b3_pa,
        )

    mom_ts = _to_utc(raw_ts)
    try:
        age = int(existing_mom_row.get("candles_after_price_action_confirmation") or 2)
    except (TypeError, ValueError):
        age = 2

    # Decision times strictly after PA up to (and including) baseline mom confirm.
    idx = pd.DatetimeIndex(pd.to_datetime(list(decision_index), utc=True)).sort_values()
    path_times = [t for t in idx if pa_ts < t <= mom_ts]
    if not path_times:
        path_times = [mom_ts]

    # Document confirmation candles along the path (after PA).
    for i, ct in enumerate(path_times, start=1):
        r2_c = lookup_asof(r2_timeline, ct) if cfg.use_r2 else {}
        b3_c = lookup_asof(b3_timeline, ct) if cfg.use_b3 else {}
        if cfg.use_r2 and _elevated(_rs(r2_c), side):
            required = max(required, int(cfg.confirm_candles_elevated))
        candle = _candle_at(candles_5m, ct) or {}
        ok, quality, mq = momentum_candle_ok_and_quality(candle, side, None)
        br = _gate_reasons(
            side=side, stage="confirm", r2_row=r2_c, b3_row=b3_c,
            use_b3=cfg.use_b3, use_r2=cfg.use_r2,
        )
        decision = "abort" if br else ("baseline_mom_reached" if ct == mom_ts else "continue")
        confirms.append(
            {
                "setup_id": setup_id,
                "variant": cfg.variant,
                "candle_index": i,
                "timestamp": str(ct),
                "open": candle.get("open"),
                "high": candle.get("high"),
                "low": candle.get("low"),
                "close": candle.get("close"),
                "momentum_ok": ok,
                "momentum_quality": quality,
                "momentum_metrics": mq,
                "risk_state": _rs(r2_c) if cfg.use_r2 else None,
                "risk_score": _score(r2_c, side) if cfg.use_r2 else None,
                "b3_state": _b3(b3_c) or _b3(r2_c),
                "decision": decision,
                "final_state_after": "ABORTED_DURING_CONFIRMATION" if br else "WAITING_CONFIRMATION",
            }
        )
        if br:
            note("ABORTED_DURING_CONFIRMATION", ct, stage=f"confirm_{i}", reasons=list(br))
            return _pack(
                setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
                existing_mom_row=existing_mom_row, cfg=cfg,
                final_state="ABORTED_DURING_CONFIRMATION", abort_reasons=br,
                state_path=path, confirm_records=confirms, entry_ts=None, entry_price=None,
                required=required, elevated_setup=elev_s, elevated_pa=elev_p, prevented=True,
                score_pa=score_pa, candles_5m=candles_5m, r2_setup=r2_s, b3_setup=b3_s,
                r2_pa=r2_pa, b3_pa=b3_pa,
            )

    # Baseline momentum reached without gate abort.
    # If elevated requires 3 candles but baseline confirmed earlier, extend.
    n_path = len(path_times)
    if required >= 3 and n_path < 3:
        extra_times = confirm_times_after(idx, mom_ts, n=3 - n_path)
        for j, ct in enumerate(extra_times, start=1):
            if ct is None:
                note("EXPIRED", mom_ts, stage="confirm_extend", reason="missing_third_candle")
                return _pack(
                    setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
                    existing_mom_row=existing_mom_row, cfg=cfg, final_state="EXPIRED",
                    abort_reasons=["CONFIRMATION_CANDLE_MISSING"], state_path=path,
                    confirm_records=confirms, entry_ts=None, entry_price=None,
                    required=required, elevated_setup=elev_s, elevated_pa=elev_p,
                    prevented=False, score_pa=score_pa, candles_5m=candles_5m,
                    r2_setup=r2_s, b3_setup=b3_s, r2_pa=r2_pa, b3_pa=b3_pa,
                )
            i = n_path + j
            r2_c = lookup_asof(r2_timeline, ct) if cfg.use_r2 else {}
            b3_c = lookup_asof(b3_timeline, ct) if cfg.use_b3 else {}
            candle = _candle_at(candles_5m, ct) or {}
            ok, quality, mq = momentum_candle_ok_and_quality(candle, side, None)
            br = _gate_reasons(
                side=side, stage="confirm", r2_row=r2_c, b3_row=b3_c,
                use_b3=cfg.use_b3, use_r2=cfg.use_r2,
            )
            decision = "abort" if br else "continue"
            confirms.append(
                {
                    "setup_id": setup_id,
                    "variant": cfg.variant,
                    "candle_index": i,
                    "timestamp": str(ct),
                    "open": candle.get("open"),
                    "high": candle.get("high"),
                    "low": candle.get("low"),
                    "close": candle.get("close"),
                    "momentum_ok": ok,
                    "momentum_quality": quality,
                    "momentum_metrics": mq,
                    "risk_state": _rs(r2_c) if cfg.use_r2 else None,
                    "risk_score": _score(r2_c, side) if cfg.use_r2 else None,
                    "b3_state": _b3(b3_c) or _b3(r2_c),
                    "decision": decision,
                    "final_state_after": "ABORTED_DURING_CONFIRMATION" if br else "WAITING_CONFIRMATION_3",
                    "note": "elevated_extension_beyond_baseline_mom",
                }
            )
            if br:
                note("ABORTED_DURING_CONFIRMATION", ct, stage=f"confirm_{i}", reasons=list(br))
                return _pack(
                    setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
                    existing_mom_row=existing_mom_row, cfg=cfg,
                    final_state="ABORTED_DURING_CONFIRMATION", abort_reasons=br,
                    state_path=path, confirm_records=confirms, entry_ts=None, entry_price=None,
                    required=required, elevated_setup=elev_s, elevated_pa=elev_p, prevented=True,
                    score_pa=score_pa, candles_5m=candles_5m, r2_setup=r2_s, b3_setup=b3_s,
                    r2_pa=r2_pa, b3_pa=b3_pa,
                )
            # Last extension candle: apply C4/C5 clearance rules
            if j == len([t for t in extra_times if t is not None]):
                r2_last = r2_c
                q_last = quality
                if cfg.variant == "C4" and cfg.use_r2 and _rs(r2_last) != "normal":
                    note(
                        "ABORTED_DURING_CONFIRMATION",
                        ct,
                        stage="confirm_3",
                        reasons=[ABORT_REASONS["RISK_ELEVATED_NOT_CLEARED"]],
                    )
                    return _pack(
                        setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
                        existing_mom_row=existing_mom_row, cfg=cfg,
                        final_state="ABORTED_DURING_CONFIRMATION",
                        abort_reasons=[ABORT_REASONS["RISK_ELEVATED_NOT_CLEARED"]],
                        state_path=path, confirm_records=confirms, entry_ts=None, entry_price=None,
                        required=required, elevated_setup=elev_s, elevated_pa=elev_p, prevented=True,
                        score_pa=score_pa, candles_5m=candles_5m, r2_setup=r2_s, b3_setup=b3_s,
                        r2_pa=r2_pa, b3_pa=b3_pa,
                    )
                if cfg.variant == "C5" and cfg.use_r2:
                    now = _score(r2_last, side)
                    drop_ok = score_pa is None or (
                        now is not None and (score_pa - now) >= float(cfg.c5_min_score_drop)
                    )
                    strong = q_last in {"improving", "stable"}
                    if not drop_ok:
                        return _pack(
                            setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
                            existing_mom_row=existing_mom_row, cfg=cfg,
                            final_state="ABORTED_DURING_CONFIRMATION",
                            abort_reasons=[ABORT_REASONS["C5_SCORE_DROP_NOT_MET"]],
                            state_path=path, confirm_records=confirms, entry_ts=None, entry_price=None,
                            required=required, elevated_setup=elev_s, elevated_pa=elev_p, prevented=True,
                            score_pa=score_pa, candles_5m=candles_5m, r2_setup=r2_s, b3_setup=b3_s,
                            r2_pa=r2_pa, b3_pa=b3_pa,
                        )
                    if cfg.c5_require_strong_momentum and not strong:
                        return _pack(
                            setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
                            existing_mom_row=existing_mom_row, cfg=cfg,
                            final_state="ABORTED_DURING_CONFIRMATION",
                            abort_reasons=[ABORT_REASONS["C5_MOMENTUM_NOT_STRONG"]],
                            state_path=path, confirm_records=confirms, entry_ts=None, entry_price=None,
                            required=required, elevated_setup=elev_s, elevated_pa=elev_p, prevented=True,
                            score_pa=score_pa, candles_5m=candles_5m, r2_setup=r2_s, b3_setup=b3_s,
                            r2_pa=r2_pa, b3_pa=b3_pa,
                        )
                entry_ts = ct
                entry_price = _entry_price(candles_5m, ct)
                st = "ENTRY_ALLOWED_AFTER_3"
                note(st, ct, stage="confirm_3", elevated_extension=True)
                confirms[-1]["decision"] = "entry"
                confirms[-1]["final_state_after"] = st
                return _pack(
                    setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
                    existing_mom_row=existing_mom_row, cfg=cfg, final_state=st, abort_reasons=[],
                    state_path=path, confirm_records=confirms, entry_ts=entry_ts,
                    entry_price=entry_price, required=required, elevated_setup=elev_s,
                    elevated_pa=elev_p, prevented=False, score_pa=score_pa, candles_5m=candles_5m,
                    r2_setup=r2_s, b3_setup=b3_s, r2_pa=r2_pa, b3_pa=b3_pa,
                )

    # Entry at baseline momentum time (gates clear; required already satisfied or ==2)
    # If required==3 but path already has >=3 bars after PA, apply C4/C5 at mom_ts.
    r2_e = lookup_asof(r2_timeline, mom_ts) if cfg.use_r2 else {}
    if required >= 3 and n_path >= 3:
        if cfg.variant == "C4" and cfg.use_r2 and _rs(r2_e) != "normal":
            note(
                "ABORTED_DURING_CONFIRMATION",
                mom_ts,
                reasons=[ABORT_REASONS["RISK_ELEVATED_NOT_CLEARED"]],
            )
            return _pack(
                setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
                existing_mom_row=existing_mom_row, cfg=cfg,
                final_state="ABORTED_DURING_CONFIRMATION",
                abort_reasons=[ABORT_REASONS["RISK_ELEVATED_NOT_CLEARED"]],
                state_path=path, confirm_records=confirms, entry_ts=None, entry_price=None,
                required=required, elevated_setup=elev_s, elevated_pa=elev_p, prevented=True,
                score_pa=score_pa, candles_5m=candles_5m, r2_setup=r2_s, b3_setup=b3_s,
                r2_pa=r2_pa, b3_pa=b3_pa,
            )

    st = "ENTRY_ALLOWED_AFTER_3" if (required >= 3 or age >= 3 or n_path >= 3) else "ENTRY_ALLOWED_AFTER_2"
    # Prefer AFTER_2 when required==2
    if required <= 2:
        st = "ENTRY_ALLOWED_AFTER_2"
    elif required >= 3:
        st = "ENTRY_ALLOWED_AFTER_3"
    note(st, mom_ts, stage="momentum", candles_after=age, required=required)
    if confirms:
        confirms[-1]["decision"] = "entry"
        confirms[-1]["final_state_after"] = st
    return _pack(
        setup_id=setup_id, side=side, setup_ts=setup_ts, pa_row=pa_row,
        existing_mom_row=existing_mom_row, cfg=cfg, final_state=st, abort_reasons=[],
        state_path=path, confirm_records=confirms, entry_ts=mom_ts,
        entry_price=_entry_price(candles_5m, mom_ts), required=required,
        elevated_setup=elev_s, elevated_pa=elev_p, prevented=False, score_pa=score_pa,
        candles_5m=candles_5m, r2_setup=r2_s, b3_setup=b3_s, r2_pa=r2_pa, b3_pa=b3_pa,
    )
