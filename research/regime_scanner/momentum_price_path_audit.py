"""Price-path / swing-sequence audit for Momentum-confirmed signals.

Research-only. Reconstructs adverse↔favorable legs after the Momentum
confirmation candle. No entry / TP / SL / hedge simulation / live changes.

Swing definition (default)
--------------------------
After the measurement candle, walk future 5m candles causally. The first leg
is always **adverse** relative to signal side. A pending extreme updates only
on a *strictly* better adverse (or favorable) price. The extreme is confirmed
once price moves at least ``swing_min_pct`` (default 0.10%) back from that
extreme toward the opposite direction. Equal highs/lows never flip the swing.
Confirmation of a swing may use later bars (post-hoc audit), but no bar before
the measurement candle is used.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .data_loader import load_symbol_candles
from .momentum_forward_audit import (
    COHORT_MOMENTUM_CONFIRMED,
    _candle_maps,
    _ts_str,
    build_signal_rows,
    excursion_pcts_for_candle,
    load_pipeline_artifacts,
    ohlc_valid,
)
from .momentum_forward_robustness import _age_is
from .point_audit import json_safe
from .signal_tp_audit import prepare_candle_window

SWING_MIN_PCT_DEFAULT = 0.10
PRIMARY_HORIZON = 96
HORIZONS = (12, 24, 48, 96)
FAVORABLE_LEVELS = (0.25, 0.50, 0.75, 1.00)
ADVERSE_THRESHOLDS = (0.50, 1.00, 1.20, 1.30, 1.50)
REF_SIGNAL_CLOSE = "signal_close"
REF_NEXT_OPEN = "next_open"

CLASS_DIRECT = "direct_favorable"
CLASS_DROP_RECOVER = "drop_then_recover"
CLASS_DROP_RECOVER_HIGHER = "drop_recover_drop_higher_recovery"
CLASS_DEEP_DROP_RECOVER = "deep_drop_then_recover"
CLASS_MULTI_SWING = "multi_swing_recovery"
CLASS_NEVER_025 = "never_recovered_025"
CLASS_INSUFFICIENT = "insufficient_future_data"


def _pct_move(from_price: float, to_price: float) -> float:
    if from_price == 0.0:
        raise ValueError("from_price must be non-zero")
    return (to_price - from_price) / abs(from_price) * 100.0


def directional_adverse_pct(*, side: str, reference: float, price: float) -> float:
    """Positive when price moved against the signal vs reference."""
    raw = _pct_move(reference, price)
    return (-raw) if side == "long" else raw


def directional_favorable_pct(*, side: str, reference: float, price: float) -> float:
    """Positive when price moved with the signal vs reference."""
    return -directional_adverse_pct(side=side, reference=reference, price=price)


def is_better_adverse(*, side: str, candidate: float, current: float) -> bool:
    """Strictly more adverse (long: lower; short: higher)."""
    if side == "long":
        return candidate < current
    if side == "short":
        return candidate > current
    raise ValueError(side)


def is_better_favorable(*, side: str, candidate: float, current: float) -> bool:
    if side == "long":
        return candidate > current
    if side == "short":
        return candidate < current
    raise ValueError(side)


def recovery_from_extreme_pct(*, side: str, extreme: float, price: float) -> float:
    """Favorable move from an adverse extreme (or adverse move from fav extreme)."""
    raw = _pct_move(extreme, price)
    return raw if side == "long" else -raw


def adverse_from_favorable_extreme_pct(*, side: str, extreme: float, price: float) -> float:
    raw = _pct_move(extreme, price)
    return -raw if side == "long" else raw


@dataclass
class SwingPoint:
    kind: str  # "adverse" | "favorable"
    leg_index: int  # 1-based within kind
    price: float
    age: int  # 0-based future candle index
    timestamp: str | None
    confirmed_at_age: int
    from_reference_pct: float
    from_prev_extreme_pct: float


@dataclass
class SwingConfig:
    swing_min_pct: float = SWING_MIN_PCT_DEFAULT
    max_candles: int = PRIMARY_HORIZON


def detect_swings(
    *,
    side: str,
    reference: float,
    future_candles: list[dict[str, Any]],
    config: SwingConfig | None = None,
) -> dict[str, Any]:
    """Detect alternating adverse/favorable swings after the measurement candle.

    Returns confirmed swing points and open (unconfirmed) pending extreme.
    """
    cfg = config or SwingConfig()
    window = future_candles[: cfg.max_candles]
    if len(future_candles) < cfg.max_candles:
        return {
            "evaluable": False,
            "reason": "INSUFFICIENT_FUTURE_CANDLES",
            "swings": [],
            "pending": None,
            "available": len(future_candles),
        }
    if any(not ohlc_valid(c) for c in window):
        return {
            "evaluable": False,
            "reason": "INVALID_OHLC",
            "swings": [],
            "pending": None,
            "available": len(future_candles),
        }

    seeking = "adverse"  # first leg
    pending_price = reference
    pending_age = -1
    pending_ts: str | None = None
    # Seed pending with first candle's adverse extreme as we walk
    swings: list[SwingPoint] = []
    adv_i = 0
    fav_i = 0
    initialized = False

    for age, candle in enumerate(window):
        high = float(candle["high"])
        low = float(candle["low"])
        ts = candle.get("timestamp")

        if seeking == "adverse":
            cand_ext = low if side == "long" else high
            if not initialized:
                pending_price = cand_ext
                pending_age = age
                pending_ts = ts
                initialized = True
            elif is_better_adverse(side=side, candidate=cand_ext, current=pending_price):
                pending_price = cand_ext
                pending_age = age
                pending_ts = ts

            # Confirmation via favorable bounce from pending extreme
            bounce_price = high if side == "long" else low
            bounce = recovery_from_extreme_pct(
                side=side, extreme=pending_price, price=bounce_price
            )
            if bounce + 1e-15 >= cfg.swing_min_pct and pending_age >= 0:
                adv_i += 1
                prev = swings[-1].price if swings else reference
                swings.append(
                    SwingPoint(
                        kind="adverse",
                        leg_index=adv_i,
                        price=pending_price,
                        age=pending_age,
                        timestamp=str(pending_ts) if pending_ts is not None else None,
                        confirmed_at_age=age,
                        from_reference_pct=directional_adverse_pct(
                            side=side, reference=reference, price=pending_price
                        ),
                        from_prev_extreme_pct=directional_adverse_pct(
                            side=side, reference=prev, price=pending_price
                        )
                        if swings
                        else directional_adverse_pct(
                            side=side, reference=reference, price=pending_price
                        ),
                    )
                )
                seeking = "favorable"
                # Start favorable pending at confirming bounce extreme
                pending_price = bounce_price
                pending_age = age
                pending_ts = ts
                initialized = True
        else:
            cand_ext = high if side == "long" else low
            if is_better_favorable(side=side, candidate=cand_ext, current=pending_price):
                pending_price = cand_ext
                pending_age = age
                pending_ts = ts

            pull_price = low if side == "long" else high
            pull = adverse_from_favorable_extreme_pct(
                side=side, extreme=pending_price, price=pull_price
            )
            if pull + 1e-15 >= cfg.swing_min_pct and pending_age >= 0:
                fav_i += 1
                prev = swings[-1].price if swings else reference
                swings.append(
                    SwingPoint(
                        kind="favorable",
                        leg_index=fav_i,
                        price=pending_price,
                        age=pending_age,
                        timestamp=str(pending_ts) if pending_ts is not None else None,
                        confirmed_at_age=age,
                        from_reference_pct=directional_favorable_pct(
                            side=side, reference=reference, price=pending_price
                        ),
                        from_prev_extreme_pct=recovery_from_extreme_pct(
                            side=side, extreme=prev, price=pending_price
                        ),
                    )
                )
                seeking = "adverse"
                pending_price = pull_price
                pending_age = age
                pending_ts = ts

    return {
        "evaluable": True,
        "reason": None,
        "swings": swings,
        "pending": {
            "seeking": seeking,
            "price": pending_price,
            "age": pending_age,
            "timestamp": pending_ts,
        },
        "available": len(future_candles),
        "window_len": len(window),
    }


def build_legs_from_swings(
    swings: list[SwingPoint],
    *,
    side: str,
    reference: float,
) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    prev_price = reference
    for sw in swings:
        if sw.kind == "adverse":
            name = f"adverse_leg_{sw.leg_index}"
            move_from_prev = directional_adverse_pct(
                side=side, reference=prev_price, price=sw.price
            )
            move_from_ref = directional_adverse_pct(
                side=side, reference=reference, price=sw.price
            )
        else:
            name = f"favorable_leg_{sw.leg_index}"
            move_from_prev = recovery_from_extreme_pct(
                side=side, extreme=prev_price, price=sw.price
            )
            move_from_ref = directional_favorable_pct(
                side=side, reference=reference, price=sw.price
            )
        legs.append(
            {
                "leg_name": name,
                "kind": sw.kind,
                "leg_index": sw.leg_index,
                "extreme_price": sw.price,
                "age": sw.age,
                "timestamp": sw.timestamp,
                "confirmed_at_age": sw.confirmed_at_age,
                "move_from_prev_pct": move_from_prev,
                "move_from_reference_pct": move_from_ref,
                "duration_minutes": sw.age * 5 if sw.age >= 0 else None,
            }
        )
        prev_price = sw.price
    return legs


def path_mfe_mae(
    *,
    side: str,
    reference: float,
    future_candles: list[dict[str, Any]],
    horizon: int,
) -> dict[str, Any]:
    if len(future_candles) < horizon:
        return {
            "evaluable": False,
            "mfe_pct": None,
            "mae_pct": None,
            "mfe_age": None,
            "mae_age": None,
        }
    window = future_candles[:horizon]
    mfe = 0.0
    mae = 0.0
    mfe_age = None
    mae_age = None
    for age, c in enumerate(window):
        fav, adv = excursion_pcts_for_candle(
            side=side,
            reference_close=reference,
            high=float(c["high"]),
            low=float(c["low"]),
        )
        fav = max(0.0, fav)
        adv = max(0.0, adv)
        if fav > mfe:
            mfe = fav
            mfe_age = age
        if adv > mae:
            mae = adv
            mae_age = age
    return {
        "evaluable": True,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "mfe_age": mfe_age,
        "mae_age": mae_age,
    }


def classify_path(
    *,
    evaluable: bool,
    legs: list[dict[str, Any]],
    mfe_96: float | None,
    mae_96: float | None,
) -> str:
    if not evaluable:
        return CLASS_INSUFFICIENT
    adv = [L for L in legs if L["kind"] == "adverse"]
    fav = [L for L in legs if L["kind"] == "favorable"]
    first_adv = float(adv[0]["move_from_reference_pct"]) if adv else 0.0
    # max favorable vs signal
    max_fav_ref = max((float(L["move_from_reference_pct"]) for L in fav), default=0.0)
    if mfe_96 is not None:
        max_fav_ref = max(max_fav_ref, float(mfe_96))

    if max_fav_ref + 1e-15 < 0.25:
        return CLASS_NEVER_025

    if first_adv <= 0.10 + 1e-15 and max_fav_ref >= 0.25:
        # nearly direct
        if not adv or first_adv <= 0.10:
            return CLASS_DIRECT

    higher_second = False
    if len(fav) >= 2:
        higher_second = float(fav[1]["move_from_reference_pct"]) > float(
            fav[0]["move_from_reference_pct"]
        ) + 1e-15

    if len(adv) >= 2 and len(fav) >= 2 and higher_second:
        return CLASS_DROP_RECOVER_HIGHER

    if len(adv) >= 2 and len(fav) >= 2 and higher_second is False:
        # still multi swing if new high somewhere — check max fav after leg2
        if max_fav_ref > float(fav[0]["move_from_reference_pct"]) + 1e-15:
            return CLASS_MULTI_SWING

    if first_adv >= 0.50 - 1e-15 and max_fav_ref >= 0.25 - 1e-15:
        return CLASS_DEEP_DROP_RECOVER

    if first_adv > 0.10 + 1e-15 and max_fav_ref >= 0.25 - 1e-15:
        return CLASS_DROP_RECOVER

    if len(adv) >= 2 and len(fav) >= 2:
        return CLASS_MULTI_SWING

    return CLASS_DROP_RECOVER if max_fav_ref >= 0.25 else CLASS_NEVER_025


def levels_reached(fav_from_ref: float | None) -> dict[str, bool]:
    out = {}
    for lv in FAVORABLE_LEVELS:
        out[f"reached_{lv:.2f}".replace(".", "_")] = (
            fav_from_ref is not None and float(fav_from_ref) + 1e-15 >= lv
        )
    return out


def analyze_adverse_threshold_recoveries(
    *,
    side: str,
    reference: float,
    future_candles: list[dict[str, Any]],
    thresholds: Iterable[float] = ADVERSE_THRESHOLDS,
    max_candles: int = PRIMARY_HORIZON,
) -> list[dict[str, Any]]:
    """When path first hits each adverse threshold, measure subsequent recovery."""
    window = future_candles[:max_candles]
    results: list[dict[str, Any]] = []
    if len(future_candles) < max_candles:
        for thr in thresholds:
            results.append(
                {
                    "adverse_threshold_pct": float(thr),
                    "reached": False,
                    "evaluable": False,
                    "reason": "INSUFFICIENT_FUTURE_CANDLES",
                }
            )
        return results

    for thr in thresholds:
        hit_age = None
        hit_price = None
        running_mae = 0.0
        for age, c in enumerate(window):
            fav, adv = excursion_pcts_for_candle(
                side=side,
                reference_close=reference,
                high=float(c["high"]),
                low=float(c["low"]),
            )
            adv = max(0.0, adv)
            if adv > running_mae:
                running_mae = adv
            if hit_age is None and adv + 1e-15 >= float(thr):
                hit_age = age
                # adverse extreme price on this candle
                hit_price = float(c["low"]) if side == "long" else float(c["high"])
                break
        if hit_age is None or hit_price is None:
            results.append(
                {
                    "adverse_threshold_pct": float(thr),
                    "reached": False,
                    "evaluable": True,
                    "threshold_age": None,
                    "threshold_price": None,
                    "max_recovery_from_extreme_pct": None,
                    "max_recovery_from_signal_pct": None,
                    "recovery_age": None,
                    "returned_to_signal": False,
                    "reached_025_after": False,
                    "reached_050_after": False,
                    "deeper_adverse_after_recovery": False,
                    "higher_favorable_after": False,
                }
            )
            continue

        # After threshold hit: track recovery and subsequent structure
        max_rec_ext = 0.0
        max_rec_sig = directional_favorable_pct(
            side=side, reference=reference, price=hit_price
        )
        # max_rec_sig starts negative typically
        max_rec_sig = max(0.0, max_rec_sig)
        rec_age = None
        best_fav_price = hit_price
        returned = False
        after_recovery_deeper = False
        higher_fav_after = False
        recovery_confirmed = False
        recovery_extreme = hit_price
        post_rec_adverse = hit_price

        for age, c in enumerate(window[hit_age:], start=hit_age):
            high = float(c["high"])
            low = float(c["low"])
            fav_px = high if side == "long" else low
            adv_px = low if side == "long" else high

            rec_ext = recovery_from_extreme_pct(
                side=side, extreme=hit_price, price=fav_px
            )
            rec_sig = directional_favorable_pct(
                side=side, reference=reference, price=fav_px
            )
            if rec_ext > max_rec_ext:
                max_rec_ext = rec_ext
                rec_age = age
            if rec_sig > max_rec_sig:
                max_rec_sig = rec_sig
                best_fav_price = fav_px
            if rec_sig + 1e-15 >= 0.0:
                returned = True

            # Detect first recovery swing (>= swing min from hit), then check deeper adverse / higher fav
            if not recovery_confirmed:
                if is_better_favorable(side=side, candidate=fav_px, current=recovery_extreme):
                    recovery_extreme = fav_px
                bounce_adv = adverse_from_favorable_extreme_pct(
                    side=side, extreme=recovery_extreme, price=adv_px
                )
                if (
                    recovery_from_extreme_pct(
                        side=side, extreme=hit_price, price=recovery_extreme
                    )
                    + 1e-15
                    >= SWING_MIN_PCT_DEFAULT
                    and bounce_adv + 1e-15 >= SWING_MIN_PCT_DEFAULT
                ):
                    recovery_confirmed = True
                    post_rec_adverse = adv_px
            else:
                if is_better_adverse(side=side, candidate=adv_px, current=post_rec_adverse):
                    post_rec_adverse = adv_px
                    if is_better_adverse(side=side, candidate=adv_px, current=hit_price):
                        after_recovery_deeper = True
                # higher favorable after first recovery extreme
                if is_better_favorable(side=side, candidate=fav_px, current=recovery_extreme):
                    higher_fav_after = True

        results.append(
            {
                "adverse_threshold_pct": float(thr),
                "reached": True,
                "evaluable": True,
                "threshold_age": hit_age,
                "threshold_price": hit_price,
                "max_recovery_from_extreme_pct": max_rec_ext,
                "max_recovery_from_signal_pct": max_rec_sig,
                "recovery_age": rec_age,
                "recovery_duration_minutes": (rec_age - hit_age) * 5
                if rec_age is not None
                else None,
                "returned_to_signal": returned,
                "reached_025_after": max_rec_sig + 1e-15 >= 0.25,
                "reached_050_after": max_rec_sig + 1e-15 >= 0.50,
                "deeper_adverse_after_recovery": after_recovery_deeper,
                "higher_favorable_after": higher_fav_after,
                "best_favorable_price_after": best_fav_price,
            }
        )
    return results


def analyze_signal_path(
    signal: dict[str, Any],
    *,
    candles: list[dict[str, Any]],
    ts_to_i: dict[str, int],
    reference_mode: str,
    swing_min_pct: float = SWING_MIN_PCT_DEFAULT,
    max_candles: int = PRIMARY_HORIZON,
) -> dict[str, Any]:
    measure_ts = signal.get("momentum_confirmation_timestamp")
    base = {
        "setup_id": signal.get("setup_id"),
        "side": signal.get("side"),
        "pattern_type": signal.get("pattern_type"),
        "momentum_confidence": signal.get("momentum_confidence"),
        "confirmation_age": signal.get("confirmation_age"),
        "signal_timestamp": measure_ts,
        "reference_mode": reference_mode,
        "combined_regime": signal.get("combined_regime"),
    }
    if not measure_ts:
        return {**base, "evaluable": False, "reason": "MISSING_TIMESTAMP", "classification": CLASS_INSUFFICIENT}

    key = _ts_str(measure_ts)
    if key not in ts_to_i:
        return {**base, "evaluable": False, "reason": "NOT_IN_FRAME", "classification": CLASS_INSUFFICIENT}

    i0 = ts_to_i[key]
    measure = candles[i0]
    if not ohlc_valid(measure):
        return {**base, "evaluable": False, "reason": "INVALID_OHLC", "classification": CLASS_INSUFFICIENT}

    signal_close = float(measure["close"])
    if i0 + 1 >= len(candles):
        return {
            **base,
            "evaluable": False,
            "reason": "NO_NEXT_CANDLE",
            "signal_close": signal_close,
            "classification": CLASS_INSUFFICIENT,
        }
    next_open = float(candles[i0 + 1]["open"])
    reference = signal_close if reference_mode == REF_SIGNAL_CLOSE else next_open
    # Future path: candles after measurement (for next_open ref, still start after measure;
    # entry realism is only the reference price, path still uses subsequent bars).
    future = candles[i0 + 1 :]
    side = str(signal["side"])

    swing = detect_swings(
        side=side,
        reference=reference,
        future_candles=future,
        config=SwingConfig(swing_min_pct=swing_min_pct, max_candles=max_candles),
    )
    if not swing.get("evaluable"):
        return {
            **base,
            "evaluable": False,
            "reason": swing.get("reason"),
            "signal_close": signal_close,
            "next_open": next_open,
            "reference_price": reference,
            "classification": CLASS_INSUFFICIENT,
            "swings": [],
            "legs": [],
        }

    swings: list[SwingPoint] = swing["swings"]
    legs = build_legs_from_swings(swings, side=side, reference=reference)
    mm = {
        h: path_mfe_mae(
            side=side, reference=reference, future_candles=future, horizon=h
        )
        for h in HORIZONS
    }
    mfe_96 = mm[96].get("mfe_pct")
    mae_96 = mm[96].get("mae_pct")
    classification = classify_path(
        evaluable=True, legs=legs, mfe_96=mfe_96, mae_96=mae_96
    )

    adv_legs = [L for L in legs if L["kind"] == "adverse"]
    fav_legs = [L for L in legs if L["kind"] == "favorable"]
    a1 = adv_legs[0] if adv_legs else None
    f1 = fav_legs[0] if fav_legs else None
    a2 = adv_legs[1] if len(adv_legs) > 1 else None
    f2 = fav_legs[1] if len(fav_legs) > 1 else None

    second_adv_deeper = None
    if a1 and a2:
        second_adv_deeper = float(a2["move_from_reference_pct"]) > float(
            a1["move_from_reference_pct"]
        ) + 1e-15
    second_fav_higher = None
    fav_delta = None
    if f1 and f2:
        second_fav_higher = float(f2["move_from_reference_pct"]) > float(
            f1["move_from_reference_pct"]
        ) + 1e-15
        fav_delta = float(f2["move_from_reference_pct"]) - float(
            f1["move_from_reference_pct"]
        )

    thr_rows = analyze_adverse_threshold_recoveries(
        side=side,
        reference=reference,
        future_candles=future,
        max_candles=max_candles,
    )

    # Favorable after each adverse leg
    fav_after_adverse: list[dict[str, Any]] = []
    for i, al in enumerate(adv_legs):
        following = [L for L in fav_legs if L["leg_index"] >= al["leg_index"]]
        # fav leg with same index follows adverse of same index
        match = next((L for L in fav_legs if L["leg_index"] == al["leg_index"]), None)
        fav_after_adverse.append(
            {
                "after_adverse_leg": al["leg_index"],
                "max_favorable_from_ref_pct": (match or {}).get("move_from_reference_pct"),
                "max_favorable_from_extreme_pct": (match or {}).get("move_from_prev_pct"),
            }
        )

    # Deepest adverse before final max favorable
    deepest_before_max_fav = None
    if fav_legs:
        max_fav = max(fav_legs, key=lambda L: float(L["move_from_reference_pct"]))
        prior_adv = [
            L for L in adv_legs if int(L["age"]) <= int(max_fav["age"])
        ]
        if prior_adv:
            deepest_before_max_fav = max(
                prior_adv, key=lambda L: float(L["move_from_reference_pct"])
            )

    row = {
        **base,
        "evaluable": True,
        "reason": None,
        "signal_close": signal_close,
        "next_open": next_open,
        "reference_price": reference,
        "classification": classification,
        "n_adverse_legs": len(adv_legs),
        "n_favorable_legs": len(fav_legs),
        "adverse_1_pct": (a1 or {}).get("move_from_reference_pct"),
        "adverse_1_price": (a1 or {}).get("extreme_price"),
        "adverse_1_age": (a1 or {}).get("age"),
        "adverse_1_minutes": (a1 or {}).get("duration_minutes"),
        "favorable_1_from_extreme_pct": (f1 or {}).get("move_from_prev_pct"),
        "favorable_1_from_signal_pct": (f1 or {}).get("move_from_reference_pct"),
        "favorable_1_price": (f1 or {}).get("extreme_price"),
        "favorable_1_age": (f1 or {}).get("age"),
        "favorable_1_minutes": (f1 or {}).get("duration_minutes"),
        **{f"fav1_{k}": v for k, v in levels_reached((f1 or {}).get("move_from_reference_pct")).items()},
        "adverse_2_from_recovery_pct": (a2 or {}).get("move_from_prev_pct"),
        "adverse_2_from_signal_pct": (a2 or {}).get("move_from_reference_pct"),
        "adverse_2_age": (a2 or {}).get("age"),
        "adverse_2_deeper_than_1": second_adv_deeper,
        "favorable_2_from_extreme_pct": (f2 or {}).get("move_from_prev_pct"),
        "favorable_2_from_signal_pct": (f2 or {}).get("move_from_reference_pct"),
        "favorable_2_age": (f2 or {}).get("age"),
        "favorable_2_higher_than_1": second_fav_higher,
        "favorable_2_minus_1_pp": fav_delta,
        **{f"fav2_{k}": v for k, v in levels_reached((f2 or {}).get("move_from_reference_pct")).items()},
        "mfe_12": mm[12].get("mfe_pct"),
        "mae_12": mm[12].get("mae_pct"),
        "mfe_24": mm[24].get("mfe_pct"),
        "mae_24": mm[24].get("mae_pct"),
        "mfe_48": mm[48].get("mfe_pct"),
        "mae_48": mm[48].get("mae_pct"),
        "mfe_96": mfe_96,
        "mae_96": mae_96,
        "mfe_96_age": mm[96].get("mfe_age"),
        "mae_96_age": mm[96].get("mae_age"),
        "deepest_adverse_before_max_fav_pct": (deepest_before_max_fav or {}).get(
            "move_from_reference_pct"
        ),
        "reached_adverse_050": any(
            t.get("reached") and float(t["adverse_threshold_pct"]) == 0.50 for t in thr_rows
        ),
        "reached_adverse_100": any(
            t.get("reached") and float(t["adverse_threshold_pct"]) == 1.00 for t in thr_rows
        ),
        "reached_adverse_120": any(
            t.get("reached") and float(t["adverse_threshold_pct"]) == 1.20 for t in thr_rows
        ),
        "reached_adverse_130": any(
            t.get("reached") and float(t["adverse_threshold_pct"]) == 1.30 for t in thr_rows
        ),
        "reached_adverse_150": any(
            t.get("reached") and float(t["adverse_threshold_pct"]) == 1.50 for t in thr_rows
        ),
        "swings": swings,
        "legs": legs,
        "threshold_recoveries": thr_rows,
        "fav_after_adverse": fav_after_adverse,
    }
    return row


def run_price_path_audit(
    *,
    price_action_confirmations: list[dict[str, Any]],
    momentum_confirmations: list[dict[str, Any]],
    momentum_events: list[dict[str, Any]],
    candles: pd.DataFrame,
    swing_min_pct: float = SWING_MIN_PCT_DEFAULT,
    max_candles: int = PRIMARY_HORIZON,
) -> dict[str, Any]:
    signals = [
        s
        for s in build_signal_rows(
            price_action_confirmations=price_action_confirmations,
            momentum_confirmations=momentum_confirmations,
            momentum_events=momentum_events,
        )
        if s.get("cohort") == COHORT_MOMENTUM_CONFIRMED
    ]
    _, ts_to_i, candle_rows = _candle_maps(candles)

    path_rows: list[dict[str, Any]] = []
    for mode in (REF_SIGNAL_CLOSE, REF_NEXT_OPEN):
        for signal in signals:
            path_rows.append(
                analyze_signal_path(
                    signal,
                    candles=candle_rows,
                    ts_to_i=ts_to_i,
                    reference_mode=mode,
                    swing_min_pct=swing_min_pct,
                    max_candles=max_candles,
                )
            )

    primary = [r for r in path_rows if r.get("reference_mode") == REF_SIGNAL_CLOSE]
    return {
        "signals": signals,
        "signal_price_paths": path_rows,
        "primary_paths": primary,
        "swing_min_pct": swing_min_pct,
        "max_candles": max_candles,
    }


def _flatten_outputs(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    paths_out: list[dict[str, Any]] = []
    swings_out: list[dict[str, Any]] = []
    legs_out: list[dict[str, Any]] = []
    thr_out: list[dict[str, Any]] = []

    for r in payload.get("signal_price_paths") or []:
        flat = {k: v for k, v in r.items() if k not in {"swings", "legs", "threshold_recoveries", "fav_after_adverse"}}
        paths_out.append(flat)
        for sw in r.get("swings") or []:
            if isinstance(sw, SwingPoint):
                swings_out.append(
                    {
                        "setup_id": r.get("setup_id"),
                        "reference_mode": r.get("reference_mode"),
                        "side": r.get("side"),
                        "kind": sw.kind,
                        "leg_index": sw.leg_index,
                        "price": sw.price,
                        "age": sw.age,
                        "timestamp": sw.timestamp,
                        "confirmed_at_age": sw.confirmed_at_age,
                        "from_reference_pct": sw.from_reference_pct,
                        "from_prev_extreme_pct": sw.from_prev_extreme_pct,
                    }
                )
        for leg in r.get("legs") or []:
            legs_out.append(
                {
                    "setup_id": r.get("setup_id"),
                    "reference_mode": r.get("reference_mode"),
                    "side": r.get("side"),
                    **leg,
                }
            )
        for thr in r.get("threshold_recoveries") or []:
            thr_out.append(
                {
                    "setup_id": r.get("setup_id"),
                    "reference_mode": r.get("reference_mode"),
                    "side": r.get("side"),
                    "pattern_type": r.get("pattern_type"),
                    "momentum_confidence": r.get("momentum_confidence"),
                    **thr,
                }
            )
    return {
        "paths": paths_out,
        "swings": swings_out,
        "legs": legs_out,
        "thresholds": thr_out,
    }


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def build_classification_summary(primary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(r.get("classification") for r in primary if r.get("evaluable"))
    n = sum(counts.values()) or 1
    return [
        {"classification": k, "n": v, "share": v / n}
        for k, v in sorted(counts.items(), key=lambda x: (-x[1], str(x[0])))
    ]


def build_threshold_table(primary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Rebuild from embedded threshold rows via flatten — caller passes thr list filtered
    return []


def build_adverse_threshold_summary(thr_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    primary_thr = [t for t in thr_rows if t.get("reference_mode") == REF_SIGNAL_CLOSE]
    out: list[dict[str, Any]] = []
    for thr in ADVERSE_THRESHOLDS:
        rows = [
            t
            for t in primary_thr
            if t.get("evaluable")
            and abs(float(t.get("adverse_threshold_pct") or -1) - float(thr)) < 1e-12
            and t.get("reached")
        ]
        n_reach = len(rows)
        returned = sum(1 for t in rows if t.get("returned_to_signal"))
        r025 = sum(1 for t in rows if t.get("reached_025_after"))
        r050 = sum(1 for t in rows if t.get("reached_050_after"))
        deeper = sum(1 for t in rows if t.get("deeper_adverse_after_recovery"))
        higher = sum(1 for t in rows if t.get("higher_favorable_after"))
        recs = [
            float(t["max_recovery_from_signal_pct"])
            for t in rows
            if t.get("max_recovery_from_signal_pct") is not None
        ]
        durs = [
            float(t["recovery_duration_minutes"])
            for t in rows
            if t.get("recovery_duration_minutes") is not None
        ]
        out.append(
            {
                "adverse_threshold_pct": float(thr),
                "n_reached": n_reach,
                "n_returned_to_signal": returned,
                "n_reached_025_after": r025,
                "n_reached_050_after": r050,
                "median_max_recovery_from_signal_pct": _median(recs),
                "median_recovery_duration_minutes": _median(durs),
                "n_deeper_adverse_after_recovery": deeper,
                "n_higher_favorable_after": higher,
            }
        )
    return out


def build_audit_summary(
    payload: dict[str, Any],
    flat: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    primary = [
        r
        for r in flat["paths"]
        if r.get("reference_mode") == REF_SIGNAL_CLOSE and r.get("evaluable")
    ]
    a1 = [float(r["adverse_1_pct"]) for r in primary if r.get("adverse_1_pct") is not None]
    f1 = [
        float(r["favorable_1_from_signal_pct"])
        for r in primary
        if r.get("favorable_1_from_signal_pct") is not None
    ]
    f2 = [
        float(r["favorable_2_from_signal_pct"])
        for r in primary
        if r.get("favorable_2_from_signal_pct") is not None
    ]

    def _cnt(pred) -> int:
        return sum(1 for r in primary if pred(r))

    class_summary = build_classification_summary(primary)
    thr_summary = build_adverse_threshold_summary(flat["thresholds"])

    by_side = defaultdict(list)
    by_conf = defaultdict(list)
    by_pat = defaultdict(list)
    for r in primary:
        by_side[r.get("side")].append(r)
        by_conf[r.get("momentum_confidence")].append(r)
        by_pat[r.get("pattern_type")].append(r)

    def _slice_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "n": len(rows),
            "median_adverse_1": _median(
                [float(r["adverse_1_pct"]) for r in rows if r.get("adverse_1_pct") is not None]
            ),
            "median_fav_1": _median(
                [
                    float(r["favorable_1_from_signal_pct"])
                    for r in rows
                    if r.get("favorable_1_from_signal_pct") is not None
                ]
            ),
            "n_never_025": sum(1 for r in rows if r.get("classification") == CLASS_NEVER_025),
            "n_drop_recover_higher": sum(
                1 for r in rows if r.get("classification") == CLASS_DROP_RECOVER_HIGHER
            ),
        }

    return {
        "n_momentum_confirmed": len(payload.get("signals") or []),
        "n_evaluable_primary": len(primary),
        "swing_min_pct": payload.get("swing_min_pct"),
        "max_candles": payload.get("max_candles"),
        "n_first_adverse_ge_050": _cnt(lambda r: (r.get("adverse_1_pct") or 0) >= 0.50),
        "n_first_adverse_ge_100": _cnt(lambda r: (r.get("adverse_1_pct") or 0) >= 1.00),
        "n_path_mae_ge_120": _cnt(lambda r: (r.get("mae_96") or 0) >= 1.20),
        "n_path_mae_ge_130": _cnt(lambda r: (r.get("mae_96") or 0) >= 1.30),
        "n_reached_adverse_050": _cnt(lambda r: r.get("reached_adverse_050")),
        "n_reached_adverse_100": _cnt(lambda r: r.get("reached_adverse_100")),
        "n_reached_adverse_120": _cnt(lambda r: r.get("reached_adverse_120")),
        "n_reached_adverse_130": _cnt(lambda r: r.get("reached_adverse_130")),
        "threshold_summary": thr_summary,
        "n_drop_recover_drop_higher": _cnt(
            lambda r: r.get("classification") == CLASS_DROP_RECOVER_HIGHER
        ),
        "adverse_1_median": _median(a1),
        "adverse_1_max": max(a1) if a1 else None,
        "favorable_1_median": _median(f1),
        "favorable_1_max": max(f1) if f1 else None,
        "favorable_2_median": _median(f2),
        "favorable_2_max": max(f2) if f2 else None,
        "n_never_recovered_025": _cnt(lambda r: r.get("classification") == CLASS_NEVER_025),
        "classification_summary": class_summary,
        "by_side": {k: _slice_stats(v) for k, v in by_side.items()},
        "by_confidence": {k: _slice_stats(v) for k, v in by_conf.items()},
        "by_pattern": {k: _slice_stats(v) for k, v in by_pat.items()},
        # recovery after 0.50 / 1.00 from threshold table
        "after_050": next((t for t in thr_summary if t["adverse_threshold_pct"] == 0.50), None),
        "after_100": next((t for t in thr_summary if t["adverse_threshold_pct"] == 1.00), None),
        "after_120": next((t for t in thr_summary if t["adverse_threshold_pct"] == 1.20), None),
        "after_130": next((t for t in thr_summary if t["adverse_threshold_pct"] == 1.30), None),
    }


def format_readme(summary: dict[str, Any], primary: list[dict[str, Any]]) -> str:
    lines = [
        "# Momentum Price-Path Audit (March week)",
        "",
        "Swing definition: first leg is adverse; a pending extreme updates only on a "
        f"**strictly** better extreme; confirmed when price reverses by ≥ "
        f"**{summary.get('swing_min_pct')}%** from that extreme. Equal highs/lows do not "
        "flip swings. Primary reference = Momentum confirmation **close**. "
        "`next_open` is reported separately and never mixed into primary stats.",
        "",
        f"Evaluable confirmed signals: **{summary.get('n_evaluable_primary')}** / "
        f"{summary.get('n_momentum_confirmed')} (horizon {summary.get('max_candles')}×5m).",
        "",
        "## Tabelle 1: Alle Signale (reference = signal_close)",
        "",
        "| setup | side | conf | age | adv1% | fav1% | adv2% | fav2% | MFE96 | MAE96 | class |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in primary:
        lines.append(
            "| {setup_id} | {side} | {momentum_confidence} | {confirmation_age} | "
            "{a1} | {f1} | {a2} | {f2} | {mfe} | {mae} | {classification} |".format(
                setup_id=r.get("setup_id"),
                side=r.get("side"),
                momentum_confidence=r.get("momentum_confidence"),
                confirmation_age=r.get("confirmation_age"),
                a1=_fmt(r.get("adverse_1_pct")),
                f1=_fmt(r.get("favorable_1_from_signal_pct")),
                a2=_fmt(r.get("adverse_2_from_signal_pct")),
                f2=_fmt(r.get("favorable_2_from_signal_pct")),
                mfe=_fmt(r.get("mfe_96")),
                mae=_fmt(r.get("mae_96")),
                classification=r.get("classification"),
            )
        )

    lines.extend(
        [
            "",
            "## Tabelle 2: Nach adverse Schwelle",
            "",
            "| thr% | n reached | back to signal | +0.25 after | +0.50 after | med recovery | med minutes | deeper after | higher fav after |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for t in summary.get("threshold_summary") or []:
        lines.append(
            "| {thr} | {n} | {ret} | {r25} | {r50} | {med} | {mins} | {deep} | {hi} |".format(
                thr=t.get("adverse_threshold_pct"),
                n=t.get("n_reached"),
                ret=t.get("n_returned_to_signal"),
                r25=t.get("n_reached_025_after"),
                r50=t.get("n_reached_050_after"),
                med=_fmt(t.get("median_max_recovery_from_signal_pct")),
                mins=_fmt(t.get("median_recovery_duration_minutes")),
                deep=t.get("n_deeper_adverse_after_recovery"),
                hi=t.get("n_higher_favorable_after"),
            )
        )

    lines.extend(
        [
            "",
            "## Headline stats",
            "",
            f"- First adverse ≥0.50%: **{summary.get('n_first_adverse_ge_050')}**",
            f"- First adverse ≥1.00%: **{summary.get('n_first_adverse_ge_100')}**",
            f"- Path MAE≥1.20% / ≥1.30%: **{summary.get('n_path_mae_ge_120')}** / "
            f"**{summary.get('n_path_mae_ge_130')}**",
            f"- Classification `drop_recover_drop_higher_recovery`: "
            f"**{summary.get('n_drop_recover_drop_higher')}**",
            f"- `never_recovered_025`: **{summary.get('n_never_recovered_025')}**",
            f"- Adverse1 median/max: `{summary.get('adverse_1_median')}` / `{summary.get('adverse_1_max')}`",
            f"- Favorable1 median/max: `{summary.get('favorable_1_median')}` / `{summary.get('favorable_1_max')}`",
            f"- Favorable2 median/max: `{summary.get('favorable_2_median')}` / `{summary.get('favorable_2_max')}`",
            "",
            "### By side / confidence / pattern",
            "",
            f"```json\n{json.dumps({k: summary.get(k) for k in ('by_side','by_confidence','by_pattern')}, indent=2)}\n```",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt(v: object) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.3f}"
    except (TypeError, ValueError):
        return str(v)


def write_price_path_outputs(
    payload: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    flat = _flatten_outputs(payload)
    summary = build_audit_summary(payload, flat)
    primary = [
        r
        for r in flat["paths"]
        if r.get("reference_mode") == REF_SIGNAL_CLOSE
    ]
    class_summary = build_classification_summary(
        [r for r in primary if r.get("evaluable")]
    )

    paths = {
        "paths": out / "signal_price_paths.csv",
        "swings": out / "signal_swing_points.csv",
        "legs": out / "signal_leg_sequence.csv",
        "thr": out / "adverse_threshold_recoveries.csv",
        "class": out / "path_classification_summary.csv",
        "summary": out / "audit_summary.json",
        "readme": out / "README.md",
    }
    pd.DataFrame(json_safe(flat["paths"])).to_csv(paths["paths"], index=False)
    pd.DataFrame(json_safe(flat["swings"])).to_csv(paths["swings"], index=False)
    pd.DataFrame(json_safe(flat["legs"])).to_csv(paths["legs"], index=False)
    pd.DataFrame(json_safe(flat["thresholds"])).to_csv(paths["thr"], index=False)
    pd.DataFrame(json_safe(class_summary)).to_csv(paths["class"], index=False)
    paths["summary"].write_text(json.dumps(json_safe(summary), indent=2), encoding="utf-8")
    paths["readme"].write_text(format_readme(summary, primary), encoding="utf-8")
    payload["audit_summary"] = summary
    payload["_flat"] = flat
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Momentum price-path / swing audit.")
    p.add_argument(
        "--pipeline-dir",
        default=(
            "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
        ),
    )
    p.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_momentum_price_path_audit_march_week1",
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-03-08")
    p.add_argument("--swing-min-pct", type=float, default=SWING_MIN_PCT_DEFAULT)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    arts = load_pipeline_artifacts(args.pipeline_dir)
    raw = load_symbol_candles(args.symbol)
    prepared = prepare_candle_window(
        raw,
        start=args.start,
        end=args.end,
        history_candles=144,
        timeframes="5m,15m,30m",
    )
    payload = run_price_path_audit(
        price_action_confirmations=arts["price_action_confirmations"],
        momentum_confirmations=arts["momentum_confirmations"],
        momentum_events=arts["momentum_events"],
        candles=prepared["candles"],
        swing_min_pct=float(args.swing_min_pct),
    )
    paths = write_price_path_outputs(payload, args.output_dir)
    summary = payload["audit_summary"]
    print(
        f"Price-path audit: n={summary.get('n_evaluable_primary')} "
        f"adv>=0.5={summary.get('n_first_adverse_ge_050')} "
        f"never025={summary.get('n_never_recovered_025')} "
        f"higher_rec={summary.get('n_drop_recover_drop_higher')}"
    )
    for path in paths.values():
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
