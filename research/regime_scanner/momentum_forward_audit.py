"""Forward-outcome audit for Phase-3 Momentum vs PA-only signals.

Pure research analytics. Does **not** change Regime / PA / Momentum logic.
Does **not** create entries, TP, or stops — only forward price statistics after
a fixed measurement candle.

Measurement bases
-----------------
* ``momentum_candle``: close of the MomentumConfirmation candle (confirmed cohort)
* ``pa_candle``: close of the PriceActionConfirmation / structure-break candle

Primary reporting basis (see README):
* confirmed → ``momentum_candle``
* invalidated / expired / not_confirmed → ``pa_candle``

Both bases are always computed when the timestamp exists so fairness can be inspected.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .data_loader import load_symbol_candles
from .point_audit import json_safe
from .signal_tp_audit import prepare_candle_window

DEFAULT_HORIZONS: tuple[int, ...] = (1, 3, 5, 12, 24, 48)

COHORT_MOMENTUM_CONFIRMED = "momentum_confirmed"
COHORT_MOMENTUM_INVALIDATED = "momentum_invalidated"
COHORT_MOMENTUM_EXPIRED = "momentum_expired"
COHORT_MOMENTUM_NOT_CONFIRMED = "momentum_not_confirmed"

PRIMARY_BASIS_BY_COHORT = {
    COHORT_MOMENTUM_CONFIRMED: "momentum_candle",
    COHORT_MOMENTUM_INVALIDATED: "pa_candle",
    COHORT_MOMENTUM_EXPIRED: "pa_candle",
    COHORT_MOMENTUM_NOT_CONFIRMED: "pa_candle",
}


def _ts_str(value: object) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return str(ts.isoformat())


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def ohlc_valid(candle: dict[str, Any]) -> bool:
    o = _finite(candle.get("open"))
    h = _finite(candle.get("high"))
    l = _finite(candle.get("low"))
    c = _finite(candle.get("close"))
    if None in (o, h, l, c):
        return False
    assert h is not None and l is not None
    return h >= l


def directional_close_return_pct(
    *,
    side: str,
    reference_close: float,
    future_close: float,
) -> float:
    """Long: up is positive; Short: down is positive. Percent."""
    if reference_close == 0.0:
        raise ValueError("reference_close must be non-zero")
    raw = (future_close - reference_close) / abs(reference_close) * 100.0
    if side == "long":
        return raw
    if side == "short":
        return -raw
    raise ValueError(f"invalid side: {side}")


def excursion_pcts_for_candle(
    *,
    side: str,
    reference_close: float,
    high: float,
    low: float,
) -> tuple[float, float]:
    """Return (favorable_pct, adverse_pct) for one future candle vs reference close."""
    if reference_close == 0.0:
        raise ValueError("reference_close must be non-zero")
    if side == "long":
        fav = (high - reference_close) / abs(reference_close) * 100.0
        adv = (reference_close - low) / abs(reference_close) * 100.0
    elif side == "short":
        fav = (reference_close - low) / abs(reference_close) * 100.0
        adv = (high - reference_close) / abs(reference_close) * 100.0
    else:
        raise ValueError(f"invalid side: {side}")
    return fav, adv


def compute_forward_path_metrics(
    *,
    side: str,
    reference_close: float,
    future_candles: list[dict[str, Any]],
    horizon: int,
) -> dict[str, Any]:
    """Compute directional close return / MFE / MAE over ``horizon`` future candles.

    ``future_candles`` must be the sequence *after* the measurement candle
    (index 0 = first candle after measurement). Uses only the first ``horizon``
    candles. Does not read any candle before that window.
    """
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if len(future_candles) < horizon:
        return {
            "evaluable": False,
            "reason": "INSUFFICIENT_FUTURE_CANDLES",
            "horizon": horizon,
            "available_future_candles": len(future_candles),
            "directional_close_return_pct": None,
            "mfe_pct": None,
            "mae_pct": None,
            "mfe_before_mae": None,
            "mfe_peak_offset": None,
            "mae_peak_offset": None,
            "invalid_ohlc_count": 0,
        }

    window = future_candles[:horizon]
    invalid = 0
    for candle in window:
        if not ohlc_valid(candle):
            invalid += 1
    if invalid:
        return {
            "evaluable": False,
            "reason": "INVALID_OHLC_IN_FORWARD_WINDOW",
            "horizon": horizon,
            "available_future_candles": len(future_candles),
            "directional_close_return_pct": None,
            "mfe_pct": None,
            "mae_pct": None,
            "mfe_before_mae": None,
            "mfe_peak_offset": None,
            "mae_peak_offset": None,
            "invalid_ohlc_count": invalid,
        }

    end_close = float(window[-1]["close"])
    dret = directional_close_return_pct(
        side=side, reference_close=reference_close, future_close=end_close
    )

    mfe = 0.0
    mae = 0.0
    mfe_peak_offset: int | None = None
    mae_peak_offset: int | None = None
    for offset, candle in enumerate(window, start=1):
        fav, adv = excursion_pcts_for_candle(
            side=side,
            reference_close=reference_close,
            high=float(candle["high"]),
            low=float(candle["low"]),
        )
        # Running extrema (never negative for reporting — clip adverse/fav to >=0)
        fav = max(0.0, fav)
        adv = max(0.0, adv)
        if fav > mfe:
            mfe = fav
            mfe_peak_offset = offset
        if adv > mae:
            mae = adv
            mae_peak_offset = offset

    mfe_before: bool | None
    if mfe_peak_offset is None and mae_peak_offset is None:
        mfe_before = None
    elif mfe_peak_offset is None:
        mfe_before = False
    elif mae_peak_offset is None:
        mfe_before = True
    else:
        mfe_before = mfe_peak_offset < mae_peak_offset

    return {
        "evaluable": True,
        "reason": None,
        "horizon": horizon,
        "available_future_candles": len(future_candles),
        "directional_close_return_pct": dret,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "mfe_before_mae": mfe_before,
        "mfe_peak_offset": mfe_peak_offset,
        "mae_peak_offset": mae_peak_offset,
        "invalid_ohlc_count": 0,
    }


def _terminal_momentum_outcome(
    setup_id: str,
    events: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None]:
    """Return (cohort_label_without_not_confirmed_combo, terminal_event)."""
    terminals = [
        e
        for e in events
        if e.get("setup_id") == setup_id
        and e.get("event") in {"momentum_confirmed", "invalidated", "expired", "rejected"}
    ]
    if not terminals:
        return None, None
    # Last terminal wins (should be unique)
    ev = terminals[-1]
    event = ev.get("event")
    if event == "momentum_confirmed":
        return COHORT_MOMENTUM_CONFIRMED, ev
    if event == "invalidated":
        return COHORT_MOMENTUM_INVALIDATED, ev
    if event == "expired":
        return COHORT_MOMENTUM_EXPIRED, ev
    if event == "rejected":
        return COHORT_MOMENTUM_EXPIRED, ev  # treat rejected with expired-like PA basis
    return None, ev


def build_signal_rows(
    *,
    price_action_confirmations: list[dict[str, Any]],
    momentum_confirmations: list[dict[str, Any]],
    momentum_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join PA + momentum terminal outcome into analytic signal rows."""
    mom_by_id = {str(c.get("setup_id")): c for c in momentum_confirmations}
    rows: list[dict[str, Any]] = []
    for pa in price_action_confirmations:
        sid = str(pa.get("setup_id"))
        cohort, term_ev = _terminal_momentum_outcome(sid, momentum_events)
        mom = mom_by_id.get(sid)
        if cohort is None:
            # PA confirmed but no momentum terminal logged — skip with flag via empty
            continue
        mom_row = mom if mom is not None else {}
        # Preserve confirmation_age=0: never use `x or fallback` on this field.
        confirmation_age = mom_row.get("candles_after_price_action_confirmation")
        confirmation_ts = mom_row.get("confirmation_timestamp")
        if confirmation_ts is None:
            confirmation_ts = mom_row.get("confirming_candle_timestamp")
        row = {
            "setup_id": sid,
            "side": pa.get("side"),
            "pattern_type": pa.get("pattern_type"),
            "cohort": cohort,
            "in_not_confirmed_combo": cohort
            in {COHORT_MOMENTUM_INVALIDATED, COHORT_MOMENTUM_EXPIRED},
            "pa_structure_break_timestamp": pa.get("structure_break_timestamp"),
            "momentum_confirmation_timestamp": confirmation_ts,
            "momentum_confidence": mom_row.get("confidence"),
            "confirmation_age": confirmation_age,
            "confirmation_type": mom_row.get("confirmation_type"),
            "regime_5m": pa.get("regime_5m"),
            "regime_15m": pa.get("regime_15m"),
            "regime_30m": pa.get("regime_30m"),
            "combined_regime": pa.get("combined_regime"),
            "pa_warnings": list(pa.get("warnings") or []),
            "momentum_terminal_reason": (term_ev or {}).get("reason"),
            "momentum_terminal_event": (term_ev or {}).get("event"),
        }
        rows.append(row)
    return rows


def _candle_maps(frame: pd.DataFrame) -> tuple[list[str], dict[str, int], list[dict[str, Any]]]:
    ts_list = [_ts_str(t) for t in frame["timestamp"].tolist()]
    ts_to_i = {t: i for i, t in enumerate(ts_list)}
    candles: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        candles.append(
            {
                "timestamp": _ts_str(row["timestamp"]),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"] if "volume" in row.index else None,
            }
        )
    return ts_list, ts_to_i, candles


def evaluate_signal_horizons(
    signal: dict[str, Any],
    *,
    candles: list[dict[str, Any]],
    ts_to_i: dict[str, int],
    horizons: Iterable[int],
    measurement_basis: str,
) -> list[dict[str, Any]]:
    """Evaluate all horizons for one signal and one measurement basis."""
    if measurement_basis == "momentum_candle":
        measure_ts = signal.get("momentum_confirmation_timestamp")
    elif measurement_basis == "pa_candle":
        measure_ts = signal.get("pa_structure_break_timestamp")
    else:
        raise ValueError(f"unknown measurement_basis: {measurement_basis}")

    out: list[dict[str, Any]] = []
    base_meta = {
        "setup_id": signal.get("setup_id"),
        "side": signal.get("side"),
        "pattern_type": signal.get("pattern_type"),
        "cohort": signal.get("cohort"),
        "in_not_confirmed_combo": signal.get("in_not_confirmed_combo"),
        "measurement_basis": measurement_basis,
        "measurement_timestamp": measure_ts,
        "momentum_confidence": signal.get("momentum_confidence"),
        "confirmation_age": signal.get("confirmation_age"),
        "confirmation_type": signal.get("confirmation_type"),
        "regime_5m": signal.get("regime_5m"),
        "regime_15m": signal.get("regime_15m"),
        "regime_30m": signal.get("regime_30m"),
        "combined_regime": signal.get("combined_regime"),
        "is_primary_basis": PRIMARY_BASIS_BY_COHORT.get(str(signal.get("cohort")))
        == measurement_basis,
    }

    if not measure_ts:
        for h in horizons:
            out.append(
                {
                    **base_meta,
                    "horizon": int(h),
                    "evaluable": False,
                    "reason": "MISSING_MEASUREMENT_TIMESTAMP",
                    "directional_close_return_pct": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "mfe_before_mae": None,
                    "reference_close": None,
                    "invalid_ohlc_count": 0,
                    "available_future_candles": 0,
                }
            )
        return out

    key = _ts_str(measure_ts)
    if key not in ts_to_i:
        for h in horizons:
            out.append(
                {
                    **base_meta,
                    "horizon": int(h),
                    "evaluable": False,
                    "reason": "MEASUREMENT_CANDLE_NOT_IN_FRAME",
                    "directional_close_return_pct": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "mfe_before_mae": None,
                    "reference_close": None,
                    "invalid_ohlc_count": 0,
                    "available_future_candles": 0,
                }
            )
        return out

    i0 = ts_to_i[key]
    measure_candle = candles[i0]
    if not ohlc_valid(measure_candle):
        for h in horizons:
            out.append(
                {
                    **base_meta,
                    "horizon": int(h),
                    "evaluable": False,
                    "reason": "INVALID_MEASUREMENT_OHLC",
                    "directional_close_return_pct": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "mfe_before_mae": None,
                    "reference_close": None,
                    "invalid_ohlc_count": 1,
                    "available_future_candles": max(0, len(candles) - i0 - 1),
                }
            )
        return out

    reference_close = float(measure_candle["close"])
    future = candles[i0 + 1 :]  # strictly after measurement — no lookahead into past

    for h in horizons:
        metrics = compute_forward_path_metrics(
            side=str(signal["side"]),
            reference_close=reference_close,
            future_candles=future,
            horizon=int(h),
        )
        out.append(
            {
                **base_meta,
                "reference_close": reference_close,
                **metrics,
            }
        )
    return out


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    w = pos - lo
    return sorted_vals[lo] * (1.0 - w) + sorted_vals[hi] * w


def aggregate_group(
    rows: list[dict[str, Any]],
    *,
    group_keys: dict[str, Any],
) -> dict[str, Any]:
    evaluable = [r for r in rows if r.get("evaluable") is True]
    n = len(evaluable)
    if n == 0:
        return {
            **group_keys,
            "n_rows": len(rows),
            "n_evaluable": 0,
            "n_not_evaluable": len(rows),
            "positive_directional_return_share": None,
            "directional_close_return_mean": None,
            "directional_close_return_median": None,
            "directional_close_return_p25": None,
            "directional_close_return_p75": None,
            "mfe_mean": None,
            "mfe_median": None,
            "mfe_p25": None,
            "mfe_p75": None,
            "mae_mean": None,
            "mae_median": None,
            "mae_p25": None,
            "mae_p75": None,
            "mfe_before_mae_share": None,
        }

    dret = sorted(float(r["directional_close_return_pct"]) for r in evaluable)
    mfe = sorted(float(r["mfe_pct"]) for r in evaluable)
    mae = sorted(float(r["mae_pct"]) for r in evaluable)
    pos = sum(1 for x in dret if x > 0.0)
    mfe_first = [
        r["mfe_before_mae"]
        for r in evaluable
        if r.get("mfe_before_mae") is not None
    ]
    mfe_first_share = (
        sum(1 for x in mfe_first if x is True) / len(mfe_first) if mfe_first else None
    )
    return {
        **group_keys,
        "n_rows": len(rows),
        "n_evaluable": n,
        "n_not_evaluable": len(rows) - n,
        "positive_directional_return_share": pos / n,
        "directional_close_return_mean": sum(dret) / n,
        "directional_close_return_median": _percentile(dret, 0.5),
        "directional_close_return_p25": _percentile(dret, 0.25),
        "directional_close_return_p75": _percentile(dret, 0.75),
        "mfe_mean": sum(mfe) / n,
        "mfe_median": _percentile(mfe, 0.5),
        "mfe_p25": _percentile(mfe, 0.25),
        "mfe_p75": _percentile(mfe, 0.75),
        "mae_mean": sum(mae) / n,
        "mae_median": _percentile(mae, 0.5),
        "mae_p25": _percentile(mae, 0.25),
        "mae_p75": _percentile(mae, 0.75),
        "mfe_before_mae_share": mfe_first_share,
    }


def build_group_summaries(
    outcome_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate by cohort×basis×horizon and additional slices on primary basis."""
    summaries: list[dict[str, Any]] = []

    def _add(rows: list[dict[str, Any]], keys: dict[str, Any]) -> None:
        summaries.append(aggregate_group(rows, group_keys=keys))

    # Core cohort × basis × horizon
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for r in outcome_rows:
        key = (r.get("cohort"), r.get("measurement_basis"), r.get("horizon"))
        buckets[key].append(r)
    for (cohort, basis, horizon), rows in sorted(buckets.items(), key=lambda x: str(x[0])):
        _add(
            rows,
            {
                "group_type": "cohort_basis_horizon",
                "cohort": cohort,
                "measurement_basis": basis,
                "horizon": horizon,
                "side": None,
                "pattern_type": None,
                "momentum_confidence": None,
                "confirmation_age": None,
                "combined_regime": None,
            },
        )

    # Combined not-confirmed (invalidated+expired) on pa_candle
    for horizon in sorted({int(r["horizon"]) for r in outcome_rows}):
        rows = [
            r
            for r in outcome_rows
            if r.get("in_not_confirmed_combo")
            and r.get("measurement_basis") == "pa_candle"
            and int(r["horizon"]) == horizon
        ]
        _add(
            rows,
            {
                "group_type": "cohort_basis_horizon",
                "cohort": COHORT_MOMENTUM_NOT_CONFIRMED,
                "measurement_basis": "pa_candle",
                "horizon": horizon,
                "side": None,
                "pattern_type": None,
                "momentum_confidence": None,
                "confirmation_age": None,
                "combined_regime": None,
            },
        )

    # Additional slices on primary-basis rows only
    primary = [r for r in outcome_rows if r.get("is_primary_basis")]
    slice_fields = (
        ("side", "side"),
        ("pattern_type", "pattern_type"),
        ("momentum_confidence", "momentum_confidence"),
        ("confirmation_age", "confirmation_age"),
        ("combined_regime", "combined_regime"),
    )
    for field_name, _ in slice_fields:
        sub: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for r in primary:
            if r.get(field_name) is None and field_name != "confirmation_age":
                # confidence/age only meaningful for confirmed; still allow None bucket skip
                if field_name in {"momentum_confidence", "confirmation_age"}:
                    continue
            sub[
                (
                    r.get("cohort"),
                    r.get("measurement_basis"),
                    r.get("horizon"),
                    r.get(field_name),
                )
            ].append(r)
        for (cohort, basis, horizon, value), rows in sorted(sub.items(), key=lambda x: str(x[0])):
            keys = {
                "group_type": f"cohort_basis_horizon_by_{field_name}",
                "cohort": cohort,
                "measurement_basis": basis,
                "horizon": horizon,
                "side": value if field_name == "side" else None,
                "pattern_type": value if field_name == "pattern_type" else None,
                "momentum_confidence": value if field_name == "momentum_confidence" else None,
                "confirmation_age": value if field_name == "confirmation_age" else None,
                "combined_regime": value if field_name == "combined_regime" else None,
            }
            _add(rows, keys)

    return summaries


def run_forward_audit(
    *,
    price_action_confirmations: list[dict[str, Any]],
    momentum_confirmations: list[dict[str, Any]],
    momentum_events: list[dict[str, Any]],
    candles: pd.DataFrame,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
) -> dict[str, Any]:
    horizons_t = tuple(int(h) for h in horizons)
    signals = build_signal_rows(
        price_action_confirmations=price_action_confirmations,
        momentum_confirmations=momentum_confirmations,
        momentum_events=momentum_events,
    )
    _, ts_to_i, candle_rows = _candle_maps(candles)

    outcome_rows: list[dict[str, Any]] = []
    for signal in signals:
        for basis in ("pa_candle", "momentum_candle"):
            # Momentum candle basis only when a confirmation timestamp exists
            if basis == "momentum_candle" and not signal.get("momentum_confirmation_timestamp"):
                continue
            outcome_rows.extend(
                evaluate_signal_horizons(
                    signal,
                    candles=candle_rows,
                    ts_to_i=ts_to_i,
                    horizons=horizons_t,
                    measurement_basis=basis,
                )
            )

    group_summary = build_group_summaries(outcome_rows)
    audit_summary = build_audit_summary(
        signals=signals,
        outcome_rows=outcome_rows,
        group_summary=group_summary,
        horizons=horizons_t,
    )
    return {
        "signals": signals,
        "signal_forward_outcomes": outcome_rows,
        "group_summary": group_summary,
        "audit_summary": audit_summary,
    }


def _primary_cohort_horizon_stats(
    group_summary: list[dict[str, Any]],
    *,
    cohort: str,
    horizon: int,
) -> dict[str, Any] | None:
    basis = PRIMARY_BASIS_BY_COHORT[cohort]
    for row in group_summary:
        if (
            row.get("group_type") == "cohort_basis_horizon"
            and row.get("cohort") == cohort
            and row.get("measurement_basis") == basis
            and int(row.get("horizon") or -1) == horizon
            and row.get("side") is None
            and row.get("pattern_type") is None
            and row.get("momentum_confidence") is None
            and row.get("confirmation_age") is None
            and row.get("combined_regime") is None
        ):
            return row
    return None


def build_audit_summary(
    *,
    signals: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
    group_summary: list[dict[str, Any]],
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    cohort_counts = defaultdict(int)
    for s in signals:
        cohort_counts[str(s.get("cohort"))] += 1
        if s.get("in_not_confirmed_combo"):
            cohort_counts[COHORT_MOMENTUM_NOT_CONFIRMED] += 1

    # Focus horizon for narrative answers: 12 candles (~1h on 5m)
    focus_h = 12 if 12 in horizons else horizons[min(len(horizons) - 1, 3)]
    confirmed = _primary_cohort_horizon_stats(
        group_summary, cohort=COHORT_MOMENTUM_CONFIRMED, horizon=focus_h
    )
    not_conf = _primary_cohort_horizon_stats(
        group_summary, cohort=COHORT_MOMENTUM_NOT_CONFIRMED, horizon=focus_h
    )
    invalidated = _primary_cohort_horizon_stats(
        group_summary, cohort=COHORT_MOMENTUM_INVALIDATED, horizon=focus_h
    )
    expired = _primary_cohort_horizon_stats(
        group_summary, cohort=COHORT_MOMENTUM_EXPIRED, horizon=focus_h
    )

    # high vs medium at focus horizon (confirmed primary)
    high = next(
        (
            g
            for g in group_summary
            if g.get("group_type") == "cohort_basis_horizon_by_momentum_confidence"
            and g.get("cohort") == COHORT_MOMENTUM_CONFIRMED
            and g.get("momentum_confidence") == "high"
            and int(g.get("horizon") or -1) == focus_h
        ),
        None,
    )
    medium = next(
        (
            g
            for g in group_summary
            if g.get("group_type") == "cohort_basis_horizon_by_momentum_confidence"
            and g.get("cohort") == COHORT_MOMENTUM_CONFIRMED
            and g.get("momentum_confidence") == "medium"
            and int(g.get("horizon") or -1) == focus_h
        ),
        None,
    )

    age_rows = [
        g
        for g in group_summary
        if g.get("group_type") == "cohort_basis_horizon_by_confirmation_age"
        and g.get("cohort") == COHORT_MOMENTUM_CONFIRMED
        and int(g.get("horizon") or -1) == focus_h
    ]

    pattern_rows = [
        g
        for g in group_summary
        if g.get("group_type") == "cohort_basis_horizon_by_pattern_type"
        and g.get("cohort") == COHORT_MOMENTUM_CONFIRMED
        and int(g.get("horizon") or -1) == focus_h
    ]

    answers = _answer_research_questions(
        focus_horizon=focus_h,
        confirmed=confirmed,
        not_confirmed=not_conf,
        high=high,
        medium=medium,
        age_rows=age_rows,
        pattern_rows=pattern_rows,
        cohort_counts=dict(cohort_counts),
    )

    return {
        "horizons": list(horizons),
        "focus_horizon_candles": focus_h,
        "signal_counts_by_cohort": dict(cohort_counts),
        "n_outcome_rows": len(outcome_rows),
        "n_evaluable_primary_focus": (confirmed or {}).get("n_evaluable"),
        "primary_basis_note": dict(PRIMARY_BASIS_BY_COHORT),
        "focus_horizon_primary_stats": {
            "momentum_confirmed": confirmed,
            "momentum_not_confirmed": not_conf,
            "momentum_invalidated": invalidated,
            "momentum_expired": expired,
            "confidence_high": high,
            "confidence_medium": medium,
            "by_confirmation_age": age_rows,
            "by_pattern_type": pattern_rows,
        },
        "research_answers": answers,
    }


def _answer_research_questions(
    *,
    focus_horizon: int,
    confirmed: dict[str, Any] | None,
    not_confirmed: dict[str, Any] | None,
    high: dict[str, Any] | None,
    medium: dict[str, Any] | None,
    age_rows: list[dict[str, Any]],
    pattern_rows: list[dict[str, Any]],
    cohort_counts: dict[str, int],
) -> dict[str, Any]:
    def _cmp_median(a: dict[str, Any] | None, b: dict[str, Any] | None, field: str) -> str:
        if not a or not b or a.get(field) is None or b.get(field) is None:
            return "insufficient_data"
        av, bv = float(a[field]), float(b[field])
        if av > bv:
            return "yes_higher"
        if av < bv:
            return "no_lower"
        return "approx_equal"

    n_conf = int(cohort_counts.get(COHORT_MOMENTUM_CONFIRMED, 0))
    n_not = int(cohort_counts.get(COHORT_MOMENTUM_NOT_CONFIRMED, 0))
    # Descriptive comparison needs both sides; robust inference needs more.
    sample_descriptive_ok = n_conf >= 20 and n_not >= 5
    sample_ok = n_conf >= 50 and n_not >= 30

    age0 = next((r for r in age_rows if r.get("confirmation_age") == 0), None)
    age_later = [
        r
        for r in age_rows
        if r.get("confirmation_age") in {1, 2, 3} and (r.get("n_evaluable") or 0) > 0
    ]
    if age0 and age_later:
        later_med = [
            float(r["mfe_median"])
            for r in age_later
            if r.get("mfe_median") is not None
        ]
        if later_med and age0.get("mfe_median") is not None:
            later_avg = sum(later_med) / len(later_med)
            age_ans = (
                "age0_better_mfe"
                if float(age0["mfe_median"]) > later_avg
                else (
                    "age0_worse_mfe"
                    if float(age0["mfe_median"]) < later_avg
                    else "similar"
                )
            )
        else:
            age_ans = "insufficient_data"
    else:
        age_ans = "insufficient_data"

    # Which patterns benefit most: confirmed vs not_confirmed pattern delta on pos share / mfe
    # We only have confirmed-by-pattern here; report ranking by median MFE among confirmed.
    ranked = sorted(
        [r for r in pattern_rows if r.get("mfe_median") is not None],
        key=lambda r: float(r["mfe_median"]),
        reverse=True,
    )
    top_patterns = [
        {"pattern_type": r.get("pattern_type"), "mfe_median": r.get("mfe_median"), "n": r.get("n_evaluable")}
        for r in ranked
    ]

    return {
        "focus_horizon_candles": focus_horizon,
        "q1_momentum_better_median_mfe_vs_not_confirmed": _cmp_median(
            confirmed, not_confirmed, "mfe_median"
        ),
        "q2_momentum_lower_median_mae_vs_not_confirmed": (
            "yes_lower"
            if confirmed
            and not_confirmed
            and confirmed.get("mae_median") is not None
            and not_confirmed.get("mae_median") is not None
            and float(confirmed["mae_median"]) < float(not_confirmed["mae_median"])
            else (
                "no_higher"
                if confirmed
                and not_confirmed
                and confirmed.get("mae_median") is not None
                and not_confirmed.get("mae_median") is not None
                and float(confirmed["mae_median"]) > float(not_confirmed["mae_median"])
                else "insufficient_data"
                if not confirmed or not not_confirmed
                else "approx_equal"
            )
        ),
        "q3_momentum_higher_positive_directional_share": (
            "yes_higher"
            if confirmed
            and not_confirmed
            and confirmed.get("positive_directional_return_share") is not None
            and not_confirmed.get("positive_directional_return_share") is not None
            and float(confirmed["positive_directional_return_share"])
            > float(not_confirmed["positive_directional_return_share"])
            else (
                "no_lower"
                if confirmed
                and not_confirmed
                and confirmed.get("positive_directional_return_share") is not None
                and not_confirmed.get("positive_directional_return_share") is not None
                and float(confirmed["positive_directional_return_share"])
                < float(not_confirmed["positive_directional_return_share"])
                else "insufficient_data"
                if not confirmed or not not_confirmed
                else "approx_equal"
            )
        ),
        "q4_high_vs_medium_confidence": {
            "mfe_median": _cmp_median(high, medium, "mfe_median"),
            "mae_median": (
                "high_lower_mae"
                if high
                and medium
                and high.get("mae_median") is not None
                and medium.get("mae_median") is not None
                and float(high["mae_median"]) < float(medium["mae_median"])
                else (
                    "high_higher_mae"
                    if high
                    and medium
                    and high.get("mae_median") is not None
                    and medium.get("mae_median") is not None
                    and float(high["mae_median"]) > float(medium["mae_median"])
                    else "insufficient_data"
                    if not high or not medium
                    else "approx_equal"
                )
            ),
            "positive_share": _cmp_median(
                high, medium, "positive_directional_return_share"
            ),
            "n_high": (high or {}).get("n_evaluable"),
            "n_medium": (medium or {}).get("n_evaluable"),
        },
        "q5_age0_vs_later": age_ans,
        "q6_pattern_mfe_ranking_among_confirmed": top_patterns,
        "q7_sample_large_enough": sample_ok,
        "q7_sample_descriptive_ok": sample_descriptive_ok,
        "q7_note": (
            f"confirmed={n_conf}, not_confirmed={n_not}; "
            + (
                "descriptive week-sample only — not large enough for robust inference"
                if sample_descriptive_ok and not sample_ok
                else "too small even for stable descriptive comparison"
                if not sample_descriptive_ok
                else "meets descriptive+robust size heuristics (still single window)"
            )
        ),
        "numeric_focus": {
            "confirmed_mfe_median": (confirmed or {}).get("mfe_median"),
            "confirmed_mae_median": (confirmed or {}).get("mae_median"),
            "confirmed_pos_share": (confirmed or {}).get(
                "positive_directional_return_share"
            ),
            "not_confirmed_mfe_median": (not_confirmed or {}).get("mfe_median"),
            "not_confirmed_mae_median": (not_confirmed or {}).get("mae_median"),
            "not_confirmed_pos_share": (not_confirmed or {}).get(
                "positive_directional_return_share"
            ),
        },
    }


def _fmt_pct(value: object, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}%"


def format_readme(audit_summary: dict[str, Any]) -> str:
    a = audit_summary.get("research_answers") or {}
    num = a.get("numeric_focus") or {}
    focus = a.get("focus_horizon_candles")
    q1 = a.get("q1_momentum_better_median_mfe_vs_not_confirmed")
    q2 = a.get("q2_momentum_lower_median_mae_vs_not_confirmed")
    q3 = a.get("q3_momentum_higher_positive_directional_share")
    q4 = a.get("q4_high_vs_medium_confidence") or {}
    q5 = a.get("q5_age0_vs_later")
    patterns = a.get("q6_pattern_mfe_ranking_among_confirmed") or []

    q1_text = {
        "yes_higher": "Nein — Median-MFE der confirmed-Gruppe ist **nicht** höher.",
        "no_lower": (
            "Nein — confirmed Median-MFE liegt **unter** not_confirmed "
            "(kein Vorteil für Momentum-Filter auf MFE)."
        ),
        "approx_equal": "Ungefähr gleich.",
        "insufficient_data": "Unzureichende Daten.",
    }.get(str(q1), str(q1))
    q2_text = {
        "yes_lower": "Ja — confirmed Median-MAE ist **niedriger** (günstiger).",
        "no_higher": "Nein — confirmed Median-MAE ist höher (schlechter).",
        "approx_equal": "Ungefähr gleich.",
        "insufficient_data": "Unzureichende Daten.",
    }.get(str(q2), str(q2))
    q3_text = {
        "yes_higher": "Ja — höhere Quote positiver directional Close-Returns.",
        "no_lower": "Nein — confirmed Quote ist **niedriger**.",
        "approx_equal": "Ungefähr gleich.",
        "insufficient_data": "Unzureichende Daten.",
    }.get(str(q3), str(q3))
    q5_text = {
        "age0_better_mfe": "Age 0 (Break-Candle) hat **höhere** Median-MFE als spätere Ages.",
        "age0_worse_mfe": "Age 0 hat **niedrigere** Median-MFE als spätere Ages.",
        "approx_equal": "Ungefähr gleich.",
        "insufficient_data": "Unzureichende Daten.",
    }.get(str(q5), str(q5))

    pattern_lines = []
    for row in patterns:
        pattern_lines.append(
            f"  - `{row.get('pattern_type')}`: median MFE "
            f"{_fmt_pct(row.get('mfe_median'))} (n={row.get('n')})"
        )
    if not pattern_lines:
        pattern_lines = ["  - (keine)"]

    conf_mfe = _fmt_pct(num.get("confirmed_mfe_median"))
    nc_mfe = _fmt_pct(num.get("not_confirmed_mfe_median"))
    conf_mae = _fmt_pct(num.get("confirmed_mae_median"))
    nc_mae = _fmt_pct(num.get("not_confirmed_mae_median"))
    conf_pos = num.get("confirmed_pos_share")
    nc_pos = num.get("not_confirmed_pos_share")
    conf_pos_s = "n/a" if conf_pos is None else f"{100.0 * float(conf_pos):.1f}%"
    nc_pos_s = "n/a" if nc_pos is None else f"{100.0 * float(nc_pos):.1f}%"

    lines = [
        "# Momentum Forward-Outcome Audit (March week)",
        "",
        "Pure forward analytics after Phase-3 signals. No entry / TP / SL / live changes.",
        "",
        "## Cohorts",
        "",
        f"- Counts: `{audit_summary.get('signal_counts_by_cohort')}`",
        f"- Primary measurement basis: `{audit_summary.get('primary_basis_note')}`",
        f"- Focus horizon for Q&A: **{focus}** closed 5m candles (~1h)",
        "",
        "## Research answers",
        "",
        "### 1. Haben Momentum-bestätigte Signale bessere Median-MFE-Werte?",
        f"- {q1_text}",
        f"- confirmed: {conf_mfe} vs not_confirmed: {nc_mfe}",
        "",
        "### 2. Haben sie geringere Median-MAE-Werte?",
        f"- {q2_text}",
        f"- confirmed: {conf_mae} vs not_confirmed: {nc_mae}",
        "",
        "### 3. Ist die Quote positiver directional Returns höher?",
        f"- {q3_text}",
        f"- confirmed: {conf_pos_s} vs not_confirmed: {nc_pos_s}",
        "",
        "### 4. Ist `high` besser als `medium`?",
        (
            f"- MFE: `{q4.get('mfe_median')}`, MAE: `{q4.get('mae_median')}`, "
            f"positive share: `{q4.get('positive_share')}` "
            f"(n_high={q4.get('n_high')}, n_medium={q4.get('n_medium')})"
        ),
        "",
        "### 5. Sind Bestätigungen auf Age 0 besser oder schlechter als spätere?",
        f"- {q5_text}",
        "",
        "### 6. Welche PA-Typen profitieren am meisten vom Momentum-Filter?",
        "- Ranking unter `momentum_confirmed` nach Median-MFE (focus horizon):",
        *pattern_lines,
        "",
        "### 7. Ist die Stichprobe groß genug für belastbare Aussagen?",
        (
            f"- **{'Ja' if a.get('q7_sample_large_enough') else 'Nein'}** — "
            f"{a.get('q7_note')}"
        ),
        "",
        "## Method notes",
        "",
        "- Directional close return uses measurement-candle **close** → horizon close.",
        "- MFE/MAE use future **high/low** only after the measurement candle.",
        "- Long/short are mirrored (short profits when price falls).",
        "- Cases without enough future candles are marked not evaluable per horizon.",
        "- Both `pa_candle` and `momentum_candle` bases are emitted for fairness checks.",
        "",
    ]
    return "\n".join(lines)


def write_forward_audit_outputs(
    payload: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "outcomes_csv": out / "signal_forward_outcomes.csv",
        "group_csv": out / "group_summary.csv",
        "summary_json": out / "audit_summary.json",
        "readme": out / "README.md",
        "outcomes_json": out / "signal_forward_outcomes.json",
        "group_json": out / "group_summary.json",
    }
    outcomes = payload.get("signal_forward_outcomes") or []
    groups = payload.get("group_summary") or []
    summary = payload.get("audit_summary") or {}

    pd.DataFrame(json_safe(outcomes)).to_csv(paths["outcomes_csv"], index=False)
    pd.DataFrame(json_safe(groups)).to_csv(paths["group_csv"], index=False)
    paths["outcomes_json"].write_text(
        json.dumps(json_safe(outcomes), indent=2, allow_nan=False), encoding="utf-8"
    )
    paths["group_json"].write_text(
        json.dumps(json_safe(groups), indent=2, allow_nan=False), encoding="utf-8"
    )
    paths["summary_json"].write_text(
        json.dumps(json_safe(summary), indent=2, allow_nan=False), encoding="utf-8"
    )
    paths["readme"].write_text(format_readme(summary), encoding="utf-8")
    return paths


def load_pipeline_artifacts(pipeline_dir: str | Path) -> dict[str, Any]:
    root = Path(pipeline_dir)
    return {
        "price_action_confirmations": json.loads(
            (root / "price_action_confirmations.json").read_text(encoding="utf-8")
        ),
        "momentum_confirmations": json.loads(
            (root / "momentum_confirmations.json").read_text(encoding="utf-8")
        ),
        "momentum_events": json.loads(
            (root / "momentum_events.json").read_text(encoding="utf-8")
        ),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Forward-outcome audit for Momentum vs PA signals (research-only)."
    )
    p.add_argument(
        "--pipeline-dir",
        default=(
            "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
        ),
    )
    p.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_momentum_forward_audit_march_week1",
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-03-08")
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
    payload = run_forward_audit(
        price_action_confirmations=arts["price_action_confirmations"],
        momentum_confirmations=arts["momentum_confirmations"],
        momentum_events=arts["momentum_events"],
        candles=prepared["candles"],
    )
    paths = write_forward_audit_outputs(payload, args.output_dir)
    summary = payload["audit_summary"]
    print(
        f"Forward audit: signals={summary.get('signal_counts_by_cohort')} "
        f"focus_h={summary.get('focus_horizon_candles')} "
        f"answers={summary.get('research_answers')}"
    )
    for path in paths.values():
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
