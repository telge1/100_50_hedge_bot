"""Stage A: causal short-term regime (EMA9/20/59) + structural EMA200."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import (
    EMA_STRUCTURE_PERIOD,
    FLAT_LOOKBACK_BARS,
    FLAT_SLOPE_ATR_FRAC_EMA20,
    FLAT_SLOPE_ATR_FRAC_EMA59,
    NEAR_EMA20_ATR_FRAC,
    SHORT_TERM_REGIMES,
    TRANSITION_MIN_ABS_SLOPE_ATR,
    TRANSITION_MIN_SPREAD_9_59_ATR,
    TRANSITION_REQUIRE_STRUCTURE,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.stage_a import (
    CONFIRMED_DIRECTED_STATES,
    is_stacked_zone,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.indicators import (
    classify_trend,
    ema_series,
    last_closed_bar_at,
    prepare_5m_indicators,
    slope_over,
)

# Map legacy uppercase classifications → StrategySpec lowercase regime labels
_REGIME_MAP = {
    "BULLISH": "bullish",
    "BEARISH": "bearish",
    "TRANSITION": "transition",
    "RANGE": "range_compression",
    "UNDETERMINED": "undetermined",
}


def prepare_bars_with_ema200(candles_1m: pd.DataFrame) -> pd.DataFrame:
    """Reuse prepare_5m_indicators; add EMA200 without altering short-term score inputs."""
    bars = prepare_5m_indicators(candles_1m)
    if bars.empty:
        return bars
    bars = bars.copy()
    bars["ema200"] = ema_series(bars["close"], EMA_STRUCTURE_PERIOD)
    # Warmup for EMA59/ATR remains causal for short-term regime (index >= 58).
    # Separate flag for structure EMA.
    bars["ema200_warmup_ok"] = bars.index >= (EMA_STRUCTURE_PERIOD - 1)
    return bars


def map_regime_label(classification: str) -> str:
    return _REGIME_MAP.get(classification, "undetermined")


def is_flat_compression(trend_classification: str, *, atr: float | None,
                        close: float | None, ema20: float | None,
                        s20_3: float | None, s59_3: float | None) -> bool:
    """True when short-term regime is flat / range_compression (blocks candidates)."""
    if trend_classification in ("RANGE", "range_compression"):
        return True
    if atr is None or atr <= 0 or close is None or ema20 is None:
        return False
    near = abs(close - ema20) / atr < NEAR_EMA20_ATR_FRAC
    flat = (
        s20_3 is not None
        and s59_3 is not None
        and abs(s20_3) < atr * FLAT_SLOPE_ATR_FRAC_EMA20
        and abs(s59_3) < atr * FLAT_SLOPE_ATR_FRAC_EMA59
    )
    return bool(flat and near)


def _ema_stack_label(e9: float, e20: float, e59: float) -> str:
    if e9 < e20 < e59:
        return "bear"
    if e9 > e20 > e59:
        return "bull"
    return "mixed"


def _to_utc_ts(asof: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(asof)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def ema_cross_and_reorder_counts(
    bars: pd.DataFrame,
    asof: datetime,
    *,
    lookback_bars: int = FLAT_LOOKBACK_BARS,
) -> tuple[int, int]:
    """Count EMA crosses and stack reorders on closed 5m bars up to asof (causal)."""
    if bars.empty or "ema9" not in bars.columns:
        return 0, 0
    asof_ts = _to_utc_ts(asof)
    closed = bars[bars["bar_end"] <= asof_ts]
    if "warmup_ok" in closed.columns:
        closed = closed[closed["warmup_ok"] == True]  # noqa: E712
    if len(closed) < 2:
        return 0, 0
    window = closed.tail(max(2, int(lookback_bars)))
    cross = 0
    reorder = 0
    prev = window.iloc[0]
    prev_stack = _ema_stack_label(float(prev["ema9"]), float(prev["ema20"]), float(prev["ema59"]))
    for i in range(1, len(window)):
        cur = window.iloc[i]
        e9p, e20p, e59p = float(prev["ema9"]), float(prev["ema20"]), float(prev["ema59"])
        e9c, e20c, e59c = float(cur["ema9"]), float(cur["ema20"]), float(cur["ema59"])
        # Cross: sign change of (fast - slow) for 9/20 and 20/59
        if (e9p - e20p) * (e9c - e20c) < 0:
            cross += 1
        if (e20p - e59p) * (e20c - e59c) < 0:
            cross += 1
        cur_stack = _ema_stack_label(e9c, e20c, e59c)
        if cur_stack != prev_stack:
            reorder += 1
        prev_stack = cur_stack
        prev = cur
    return int(cross), int(reorder)


def flat_reason_codes(
    *,
    is_flat: bool,
    legacy_classification: str | None,
    regime: str | None,
    near_ema20: bool | None = None,
) -> list[str]:
    if not is_flat:
        return []
    codes = ["FLAT_COMPRESSION"]
    legacy = str(legacy_classification or "")
    reg = str(regime or "")
    if legacy in ("RANGE",) or reg == "range_compression":
        codes.append("RANGE_COMPRESSION")
    if near_ema20:
        codes.append("NEAR_EMA20_FLAT_SLOPES")
    return codes


def transition_quality(
    *,
    ema20_slope_3_atr: float | None,
    ema_spread_9_59_atr: float | None,
    structure: str | None,
    stacked: bool,
    touched: bool,
    clearance_wait: bool,
) -> tuple[bool, list[str]]:
    """transition may release only with slope, separation, clear zone, touch, clearance."""
    reasons: list[str] = []
    ok = True
    if not touched:
        ok = False
        reasons.append("TRANSITION_NO_TOUCH")
    if stacked:
        ok = False
        reasons.append("TRANSITION_STACKED_ZONE")
    if clearance_wait:
        ok = False
        reasons.append("TRANSITION_CLEARANCE_INSUFFICIENT")
    slope = ema20_slope_3_atr
    if slope is None or abs(float(slope)) < TRANSITION_MIN_ABS_SLOPE_ATR:
        ok = False
        reasons.append("TRANSITION_SLOPE_INSUFFICIENT")
    else:
        reasons.append("TRANSITION_SLOPE_OK")
    spread = ema_spread_9_59_atr
    if spread is None or float(spread) < TRANSITION_MIN_SPREAD_9_59_ATR:
        ok = False
        reasons.append("TRANSITION_SEPARATION_INSUFFICIENT")
    else:
        reasons.append("TRANSITION_SEPARATION_OK")
    if TRANSITION_REQUIRE_STRUCTURE and str(structure or "") not in ("HH_HL", "LH_LL"):
        ok = False
        reasons.append("TRANSITION_STRUCTURE_INSUFFICIENT")
    if ok:
        reasons.append("TRANSITION_QUALITY_PASS")
    return ok, reasons


def evaluate_regime_gate(
    *,
    regime: str,
    block_flat_compression: bool = False,
    ema20_slope_3_atr: float | None = None,
    ema_spread_9_59_atr: float | None = None,
    structure: str | None = None,
    zone_name: str = "",
    touched: bool = False,
    clearance_wait: bool = False,
) -> dict[str, Any]:
    """Stage-A regime gate (Paket 2).

    Short-term score inputs are EMA9/20/59 only. EMA200 is never required here.

    Rules:
    - bullish/bearish: allow further checks (Stage B + directed after confirm)
    - range_compression: hard block
    - undetermined: hard block for directed candidates (Stage B observation ok)
    - transition: release only if slope, separation, clear zone, touch, clearance ok
    """
    reg = str(regime or "undetermined").lower()
    if reg not in SHORT_TERM_REGIMES:
        reg = "undetermined"
    stacked = is_stacked_zone(zone_name)
    out: dict[str, Any] = {
        "regime": reg,
        "allow_stage_b": False,
        "allow_directed": False,
        "hard_block": False,
        "block_state": "",
        "reason_codes": [],
        "transition_quality_ok": None,
        "ema200_in_regime_score": False,
    }

    if block_flat_compression or reg == "range_compression":
        out.update(
            {
                "allow_stage_b": False,
                "allow_directed": False,
                "hard_block": True,
                "block_state": "block_flat_compression",
                "reason_codes": ["BLOCK_FLAT_COMPRESSION", "REGIME_GATE_RANGE_COMPRESSION"],
            }
        )
        return out

    if reg == "undetermined":
        out.update(
            {
                "allow_stage_b": True,
                "allow_directed": False,
                "hard_block": True,
                "block_state": "",
                "reason_codes": ["BLOCK_UNDETERMINED_REGIME_DIRECTED"],
            }
        )
        return out

    if reg in ("bullish", "bearish"):
        out.update(
            {
                "allow_stage_b": True,
                "allow_directed": True,
                "hard_block": False,
                "reason_codes": [f"REGIME_GATE_{reg.upper()}_ALLOW"],
            }
        )
        return out

    # transition
    tq_ok, tq_reasons = transition_quality(
        ema20_slope_3_atr=ema20_slope_3_atr,
        ema_spread_9_59_atr=ema_spread_9_59_atr,
        structure=structure,
        stacked=stacked,
        touched=touched,
        clearance_wait=clearance_wait,
    )
    out["transition_quality_ok"] = tq_ok
    out["reason_codes"] = list(tq_reasons)
    if tq_ok:
        out.update(
            {
                "allow_stage_b": True,
                "allow_directed": True,
                "hard_block": False,
            }
        )
        out["reason_codes"].append("REGIME_GATE_TRANSITION_ALLOW")
    else:
        out.update(
            {
                "allow_stage_b": False,
                "allow_directed": False,
                "hard_block": True,
                "block_state": "",
            }
        )
        out["reason_codes"].append("REGIME_GATE_TRANSITION_BLOCK")
    return out


def apply_regime_gate_to_candidate(
    *,
    final_state: str,
    reasons: list[str],
    gate: dict[str, Any],
) -> tuple[str, list[str], bool]:
    """Demote directed Stage-B states when regime gate forbids directed emit.

    Returns (final_state, reasons, allow_directed).
    """
    allow_directed = bool(gate.get("allow_directed"))
    rc = list(reasons)
    for code in gate.get("reason_codes") or []:
        if code and code not in rc:
            rc.append(str(code))

    if gate.get("block_state") == "block_flat_compression":
        return "block_flat_compression", rc, False

    if final_state in CONFIRMED_DIRECTED_STATES and not allow_directed:
        # Hard block directed candidates; keep research-visible no_trade.
        if "BLOCK_UNDETERMINED_REGIME_DIRECTED" in rc:
            rc.append("DIRECTED_DEMOTED_UNDETERMINED_REGIME")
        elif "REGIME_GATE_TRANSITION_BLOCK" in rc:
            rc.append("DIRECTED_DEMOTED_TRANSITION_QUALITY")
        else:
            rc.append("DIRECTED_DEMOTED_REGIME_GATE")
        return "no_trade", rc, False

    return final_state, rc, allow_directed


def regime_snapshot(bars: pd.DataFrame, asof: datetime) -> dict[str, Any]:
    """Causal regime at asof using closed 5m only; EMA200 logged but not scored equally."""
    snap = classify_trend(bars, asof)
    regime = map_regime_label(snap.classification)
    row = last_closed_bar_at(bars, asof)
    ema200 = float(row["ema200"]) if row is not None and "ema200" in row.index and pd.notna(row.get("ema200")) else None
    ema200_ok = bool(row.get("ema200_warmup_ok", False)) if row is not None else False
    s9_3 = slope_over(bars, "ema9", asof, 3) if "ema9" in bars.columns else None
    s9_6 = slope_over(bars, "ema9", asof, 6) if "ema9" in bars.columns else None

    atr = snap.atr
    atr_norm = {}
    if atr and atr > 0:
        for k, v in (
            ("ema20_slope_3_atr", snap.ema20_slope_3),
            ("ema20_slope_6_atr", snap.ema20_slope_6),
            ("ema59_slope_3_atr", snap.ema59_slope_3),
            ("ema59_slope_6_atr", snap.ema59_slope_6),
            ("ema9_slope_3_atr", s9_3),
            ("ema9_slope_6_atr", s9_6),
        ):
            atr_norm[k] = (v / atr) if v is not None else None
    else:
        atr_norm = {k: None for k in (
            "ema20_slope_3_atr", "ema20_slope_6_atr",
            "ema59_slope_3_atr", "ema59_slope_6_atr",
            "ema9_slope_3_atr", "ema9_slope_6_atr",
        )}

    spread_atr = None
    if atr and atr > 0 and snap.ema9 is not None and snap.ema59 is not None:
        spread_atr = abs(snap.ema9 - snap.ema59) / atr

    flat = is_flat_compression(
        snap.classification,
        atr=snap.atr,
        close=snap.close,
        ema20=snap.ema20,
        s20_3=snap.ema20_slope_3,
        s59_3=snap.ema59_slope_3,
    )
    if flat:
        regime = "range_compression"

    # Order flip vs prior closed bar (causal)
    order_flip = "none"
    if row is not None and snap.warmup_ok:
        asof_ts = pd.Timestamp(asof)
        if asof_ts.tzinfo is None:
            asof_ts = asof_ts.tz_localize("UTC")
        else:
            asof_ts = asof_ts.tz_convert("UTC")
        asof_closed = bars[bars["bar_end"] <= asof_ts]
        if len(asof_closed) >= 2:
            prev = asof_closed.iloc[-2]

            def stack(e9, e20, e59):
                if e9 < e20 < e59:
                    return "bear"
                if e9 > e20 > e59:
                    return "bull"
                return "mixed"

            cur_s = stack(snap.ema9, snap.ema20, snap.ema59)
            prev_s = stack(float(prev["ema9"]), float(prev["ema20"]), float(prev["ema59"]))
            if cur_s != prev_s:
                order_flip = f"{prev_s}->{cur_s}"

    # Base gate without zone/touch context (timeline / diagnostics).
    base_gate = evaluate_regime_gate(
        regime=regime,
        block_flat_compression=flat,
        ema20_slope_3_atr=atr_norm.get("ema20_slope_3_atr"),
        ema_spread_9_59_atr=spread_atr,
        structure=snap.structure,
        zone_name="",
        touched=False,
        clearance_wait=False,
    )

    return {
        "asof_utc": snap.asof_utc,
        "regime": regime,
        "legacy_classification": snap.classification,
        "confidence": snap.confidence,
        "reasons": snap.reasons,
        "score_components": snap.score_components,
        "ema9": snap.ema9,
        "ema20": snap.ema20,
        "ema59": snap.ema59,
        "ema200": ema200,
        "ema200_warmup_ok": ema200_ok,
        "ema200_in_regime_score": False,
        "atr": snap.atr,
        "close": snap.close,
        "last_bar_end": snap.last_bar_end,
        "ema20_slope_3": snap.ema20_slope_3,
        "ema20_slope_6": snap.ema20_slope_6,
        "ema59_slope_3": snap.ema59_slope_3,
        "ema59_slope_6": snap.ema59_slope_6,
        "ema9_slope_3": s9_3,
        "ema9_slope_6": s9_6,
        **atr_norm,
        "ema_spread_9_59_atr": spread_atr,
        "ema_order_flip": order_flip,
        "ret_15m": snap.ret_15m,
        "ret_30m": snap.ret_30m,
        "ret_60m": snap.ret_60m,
        "structure": snap.structure,
        "warmup_ok": snap.warmup_ok,
        "block_flat_compression": flat,
        "reason_code_flat": "BLOCK_FLAT_COMPRESSION" if flat else "",
        "regime_gate_allow_stage_b_base": base_gate["allow_stage_b"] if regime != "transition" else True,
        "regime_gate_allow_directed_base": base_gate["allow_directed"] if regime in ("bullish", "bearish") else False,
        "short_term_inputs": "ema9|ema20|ema59|slopes|stack|price|structure",
        "ema200_role": "sr_clearance_flip_context",
    }


def flat_diagnostics(
    bars: pd.DataFrame,
    asof: datetime,
    *,
    snap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Paket 2B: flat metrics at a causal timestamp (watch / touch / decision)."""
    if snap is None:
        snap = regime_snapshot(bars, asof)
    is_flat = bool(snap.get("block_flat_compression"))
    atr = snap.get("atr")
    close = snap.get("close")
    ema20 = snap.get("ema20")
    near = None
    if atr and atr > 0 and close is not None and ema20 is not None:
        near = abs(float(close) - float(ema20)) / float(atr) < NEAR_EMA20_ATR_FRAC
    reasons = flat_reason_codes(
        is_flat=is_flat,
        legacy_classification=snap.get("legacy_classification"),
        regime=snap.get("regime"),
        near_ema20=near,
    )
    spread_pct = None
    if close and snap.get("ema9") is not None and snap.get("ema59") is not None and float(close) > 0:
        spread_pct = abs(float(snap["ema9"]) - float(snap["ema59"])) / float(close) * 100.0
    cross, reorder = ema_cross_and_reorder_counts(bars, asof)
    return {
        "flat": is_flat,
        "flat_reason": "|".join(reasons),
        "ema9_slope_norm": snap.get("ema9_slope_3_atr"),
        "ema20_slope_norm": snap.get("ema20_slope_3_atr"),
        "ema59_slope_norm": snap.get("ema59_slope_3_atr"),
        "ema_spread_pct": spread_pct,
        "ema_spread_9_59_atr": snap.get("ema_spread_9_59_atr"),
        "ema_cross_count": cross,
        "ema_reorder_count": reorder,
        "regime": snap.get("regime"),
        "block_flat_compression": is_flat,
    }


def flat_block_payload(
    *,
    flat_at_watch: bool | None,
    flat_at_touch: bool | None,
    flat_at_decision: bool | None,
    diag: dict[str, Any],
    decisive_stage: str,
) -> dict[str, Any]:
    """Fields when flat blocks a decisive Stage-A step."""
    block_reasons = ["FLAT_COMPRESSION"]
    fr = str(diag.get("flat_reason") or "")
    for part in fr.split("|"):
        if part and part not in block_reasons:
            block_reasons.append(part)
    return {
        "ema_setup_state": "block_flat_compression",
        "candidate_state": "block_flat_compression",
        "flat_at_watch": flat_at_watch if flat_at_watch is not None else False,
        "flat_at_touch": flat_at_touch if flat_at_touch is not None else False,
        "flat_at_decision": flat_at_decision if flat_at_decision is not None else False,
        "flat_reason": fr or "FLAT_COMPRESSION",
        "flat_decisive_stage": decisive_stage,
        "ema9_slope_norm": diag.get("ema9_slope_norm"),
        "ema20_slope_norm": diag.get("ema20_slope_norm"),
        "ema59_slope_norm": diag.get("ema59_slope_norm"),
        "ema_spread_pct": diag.get("ema_spread_pct"),
        "ema_cross_count": diag.get("ema_cross_count"),
        "ema_reorder_count": diag.get("ema_reorder_count"),
        "block_reasons": "|".join(block_reasons),
        "reason_codes": "|".join(block_reasons),
    }


def detect_ema200_flip_timestamps(
    *,
    bars: pd.DataFrame,
    samples: list[Any],
    zone200_low: float | None,
    zone200_high: float | None,
    role: str,
    mechanism: str,
    timeline: dict[str, Any],
    contact_ts_ms: int | None,
) -> dict[str, Any]:
    """Separate flip clocks; EMA cross alone never confirms breakout."""
    from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING

    out = {
        "price_breakout_at": MISSING,
        "wall_absorbed_at": timeline.get("wall_absorbed_at") or MISSING,
        "wall_pulled_at": MISSING,
        "breakout_confirmed_at": timeline.get("breakout_confirmed_at") or MISSING,
        "retest_at": timeline.get("retest_at") or MISSING,
        "fast_ema_cross_confirmed_at": MISSING,
        "full_regime_flip_confirmed_at": MISSING,
        "possible_regime_flip": False,
        "full_regime_flip_confirmed": False,
        "ema_cross_alone_not_breakout": True,
    }
    if zone200_low is None or zone200_high is None or not samples:
        return out

    # Price breakout through EMA200 band
    for s in samples:
        if role == "resistance" and s.mid > zone200_high:
            out["price_breakout_at"] = _iso_ms(s.ts_ms)
            break
        if role == "support" and s.mid < zone200_low:
            out["price_breakout_at"] = _iso_ms(s.ts_ms)
            break

    if mechanism == "LIQUIDITY_PULL":
        out["wall_pulled_at"] = timeline.get("classification_at") or (
            _iso_ms(contact_ts_ms) if contact_ts_ms else MISSING
        )

    # Fast EMA (9/20) cross EMA200 — causal closed bars only
    closed = bars[bars["warmup_ok"] == True] if "warmup_ok" in bars.columns else bars  # noqa: E712
    if "ema200" in closed.columns and len(closed) >= 2:
        for i in range(1, len(closed)):
            prev, cur = closed.iloc[i - 1], closed.iloc[i]
            e9p, e20p, e200p = float(prev["ema9"]), float(prev["ema20"]), float(prev["ema200"])
            e9c, e20c, e200c = float(cur["ema9"]), float(cur["ema20"]), float(cur["ema200"])
            if role == "resistance":
                crossed = (e9p <= e200p or e20p <= e200p) and (e9c > e200c and e20c > e200c)
            else:
                crossed = (e9p >= e200p or e20p >= e200p) and (e9c < e200c and e20c < e200c)
            if crossed:
                out["fast_ema_cross_confirmed_at"] = str(cur["bar_end"])
                break

        # Full flip: EMA59 also across EMA200 after fast cross
        if out["fast_ema_cross_confirmed_at"] != MISSING:
            after = closed[closed["bar_end"] > pd.Timestamp(out["fast_ema_cross_confirmed_at"])]
            for _, cur in after.iterrows():
                e59, e200 = float(cur["ema59"]), float(cur["ema200"])
                ok = (e59 > e200) if role == "resistance" else (e59 < e200)
                if ok:
                    out["full_regime_flip_confirmed_at"] = str(cur["bar_end"])
                    out["full_regime_flip_confirmed"] = True
                    break

    absorbed = mechanism in ("ASK_ABSORPTION", "BID_ABSORPTION")
    held = out["breakout_confirmed_at"] != MISSING
    price_bo = out["price_breakout_at"] != MISSING
    out["possible_regime_flip"] = bool(price_bo and absorbed and not out["full_regime_flip_confirmed"])
    # Full confirmed only when price + absorption/hold + later slow EMA — never cross alone
    if out["full_regime_flip_confirmed"] and not (price_bo and (absorbed or held)):
        out["full_regime_flip_confirmed"] = False
        out["full_regime_flip_confirmed_at"] = MISSING
    return out


def _iso_ms(ts_ms: int) -> str:
    from datetime import timezone

    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"
