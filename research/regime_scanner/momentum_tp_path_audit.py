"""Drawdown / intrabar path audit for Momentum TP hits (research-only).

Extends the March-week TP-hit analysis with:
* MAE before TP vs MAE including TP candle
* drawdown buckets
* follow-up on non-hits at 24/48
* optional 1m same-candle resolution (no downloads)
* optimistic / conservative / resolved-only hit rates

Does not change Regime / PA / Momentum logic. No SL / entry / live changes.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from research.backtests.candle_loader import (
    DEFAULT_DATA_DIR,
    load_candles_for_symbol,
    symbol_to_feather_name,
)

from .data_loader import load_symbol_candles
from .momentum_forward_audit import (
    COHORT_MOMENTUM_CONFIRMED,
    PRIMARY_BASIS_BY_COHORT,
    _candle_maps,
    _ts_str,
    build_signal_rows,
    compute_forward_path_metrics,
    directional_close_return_pct,
    excursion_pcts_for_candle,
    load_pipeline_artifacts,
    ohlc_valid,
)
from .momentum_forward_robustness import _age_is
from .momentum_tp_hit_audit import (
    FOCUS_HORIZON,
    GROUP_CONFIRMED_AGE0,
    GROUP_CONFIRMED_HIGH,
    GROUP_CONFIRMED_HIGH_AGE0,
    GROUP_MOMENTUM_CONFIRMED,
    GROUP_MOMENTUM_NOT_CONFIRMED,
    GROUP_ORDER,
    PRIMARY_TP_PCT,
    signal_groups,
)
from .point_audit import json_safe
from .signal_tp_audit import prepare_candle_window

DRAWDOWN_BUCKETS: tuple[tuple[str, float | None, float | None], ...] = (
    ("0.00-0.25", 0.0, 0.25),
    ("0.25-0.50", 0.25, 0.50),
    ("0.50-0.75", 0.50, 0.75),
    ("0.75-1.00", 0.75, 1.00),
    ("1.00-1.50", 1.00, 1.50),
    (">1.50", 1.50, None),
)

RESOLUTION_TP_FIRST = "tp_first"
RESOLUTION_ADVERSE_FIRST = "adverse_first"
RESOLUTION_UNRESOLVED_1M = "unresolved_1m"
RESOLUTION_MISSING_1M = "missing_1m_data"
RESOLUTION_NOT_AMBIGUOUS = "not_ambiguous"


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


def drawdown_bucket(mae_pct: float | None) -> str | None:
    if mae_pct is None:
        return None
    x = float(mae_pct)
    # Buckets: 0.00–0.25 inclusive on upper for first; then (lo, hi] for middle;
    # last is >1.50
    if x <= 0.25:
        return "0.00-0.25"
    if x <= 0.50:
        return "0.25-0.50"
    if x <= 0.75:
        return "0.50-0.75"
    if x <= 1.00:
        return "0.75-1.00"
    if x <= 1.50:
        return "1.00-1.50"
    return ">1.50"


def find_local_1m_path(
    symbol: str = "APTUSDT",
    data_dir: str | Path | None = None,
) -> Path | None:
    """Search only local project data dirs. No downloads."""
    roots = [
        Path(data_dir) if data_dir else DEFAULT_DATA_DIR,
        Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures"),
        Path("data"),
        Path("research/backtests/data"),
    ]
    name = symbol_to_feather_name(symbol, timeframe="1m")
    for root in roots:
        candidate = Path(root) / name
        if candidate.is_file():
            return candidate
        # also allow csv
        csv = candidate.with_suffix(".csv")
        if csv.is_file():
            return csv
    return None


def load_optional_1m_candles(
    symbol: str = "APTUSDT",
    data_dir: str | Path | None = None,
) -> tuple[pd.DataFrame | None, str]:
    path = find_local_1m_path(symbol, data_dir=data_dir)
    if path is None:
        return None, "missing_1m_file"
    try:
        rows = load_candles_for_symbol(symbol, timeframe="1m", data_dir=path.parent)
    except Exception as exc:  # noqa: BLE001 — research audit: record failure
        return None, f"load_failed:{type(exc).__name__}"
    if not rows:
        return None, "empty_1m"
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.sort_values("timestamp").reset_index(drop=True), str(path)


def tp_and_adverse_levels(
    *,
    side: str,
    reference_close: float,
    tp_pct: float,
    hit_candle: dict[str, Any],
) -> tuple[float, float]:
    """Return (tp_level, adverse_extreme_on_hit_candle)."""
    fav, _ = excursion_pcts_for_candle(
        side=side,
        reference_close=reference_close,
        high=float(hit_candle["high"]),
        low=float(hit_candle["low"]),
    )
    _ = fav
    if side == "long":
        tp_level = reference_close * (1.0 + tp_pct / 100.0)
        adverse_level = float(hit_candle["low"])
    elif side == "short":
        tp_level = reference_close * (1.0 - tp_pct / 100.0)
        adverse_level = float(hit_candle["high"])
    else:
        raise ValueError(f"invalid side: {side}")
    return tp_level, adverse_level


def resolve_same_candle_with_1m(
    *,
    side: str,
    reference_close: float,
    tp_pct: float,
    hit_candle_5m: dict[str, Any],
    candles_1m: pd.DataFrame | None,
) -> dict[str, Any]:
    """Resolve 5m same-candle ambiguity using 1m bars inside the 5m window.

    No tick order invention. If TP and adverse extreme occur in the same 1m bar
    → ``unresolved_1m``. If no 1m data → ``missing_1m_data``.
    """
    if candles_1m is None or candles_1m.empty:
        return {
            "resolution": RESOLUTION_MISSING_1M,
            "tp_first": False,
            "adverse_first": False,
            "n_1m_bars": 0,
        }

    start = pd.Timestamp(hit_candle_5m["timestamp"])
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    else:
        start = start.tz_convert("UTC")
    end = start + pd.Timedelta(minutes=5)
    window = candles_1m[
        (candles_1m["timestamp"] >= start) & (candles_1m["timestamp"] < end)
    ]
    if window.empty:
        return {
            "resolution": RESOLUTION_MISSING_1M,
            "tp_first": False,
            "adverse_first": False,
            "n_1m_bars": 0,
        }

    tp_level, adverse_level = tp_and_adverse_levels(
        side=side,
        reference_close=reference_close,
        tp_pct=tp_pct,
        hit_candle=hit_candle_5m,
    )

    for _, bar in window.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        if side == "long":
            hit_tp = high + 1e-15 >= tp_level
            hit_adv = low - 1e-15 <= adverse_level
        else:
            hit_tp = low - 1e-15 <= tp_level
            hit_adv = high + 1e-15 >= adverse_level
        if hit_tp and hit_adv:
            return {
                "resolution": RESOLUTION_UNRESOLVED_1M,
                "tp_first": False,
                "adverse_first": False,
                "n_1m_bars": int(len(window)),
            }
        if hit_tp:
            return {
                "resolution": RESOLUTION_TP_FIRST,
                "tp_first": True,
                "adverse_first": False,
                "n_1m_bars": int(len(window)),
            }
        if hit_adv:
            return {
                "resolution": RESOLUTION_ADVERSE_FIRST,
                "tp_first": False,
                "adverse_first": True,
                "n_1m_bars": int(len(window)),
            }

    # Neither level fully reconstructed (data gap / incomplete extremes)
    return {
        "resolution": RESOLUTION_UNRESOLVED_1M,
        "tp_first": False,
        "adverse_first": False,
        "n_1m_bars": int(len(window)),
    }


def compute_signal_tp_path(
    *,
    side: str,
    reference_close: float,
    future_candles: list[dict[str, Any]],
    horizon: int,
    tp_pct: float,
) -> dict[str, Any]:
    """Path metrics for one signal at one horizon."""
    if len(future_candles) < horizon:
        return {
            "evaluable": False,
            "reason": "INSUFFICIENT_FUTURE_CANDLES",
            "tp_hit": False,
            "same_candle_ambiguous": False,
            "first_hit_age": None,
            "mae_before_tp_pct": None,
            "mae_including_tp_candle_pct": None,
            "path_mae_pct": None,
            "hit_candle": None,
            "available_future_candles": len(future_candles),
        }

    window = future_candles[:horizon]
    if any(not ohlc_valid(c) for c in window):
        return {
            "evaluable": False,
            "reason": "INVALID_OHLC_IN_FORWARD_WINDOW",
            "tp_hit": False,
            "same_candle_ambiguous": False,
            "first_hit_age": None,
            "mae_before_tp_pct": None,
            "mae_including_tp_candle_pct": None,
            "path_mae_pct": None,
            "hit_candle": None,
            "available_future_candles": len(future_candles),
        }

    path_mae = 0.0
    mae_before = 0.0
    for age, candle in enumerate(window):
        fav, adv = excursion_pcts_for_candle(
            side=side,
            reference_close=reference_close,
            high=float(candle["high"]),
            low=float(candle["low"]),
        )
        fav = max(0.0, fav)
        adv = max(0.0, adv)
        path_mae = max(path_mae, adv)
        if fav + 1e-15 >= tp_pct:
            ambiguous = adv > 0.0
            return {
                "evaluable": True,
                "reason": None,
                "tp_hit": True,
                "same_candle_ambiguous": ambiguous,
                "first_hit_age": age,
                "mae_before_tp_pct": mae_before,
                "mae_including_tp_candle_pct": max(mae_before, adv),
                "path_mae_pct": path_mae,
                "hit_candle": candle,
                "directional_close_return_at_hit_pct": directional_close_return_pct(
                    side=side,
                    reference_close=reference_close,
                    future_close=float(candle["close"]),
                ),
                "available_future_candles": len(future_candles),
            }
        mae_before = max(mae_before, adv)

    return {
        "evaluable": True,
        "reason": None,
        "tp_hit": False,
        "same_candle_ambiguous": False,
        "first_hit_age": None,
        "mae_before_tp_pct": None,
        "mae_including_tp_candle_pct": None,
        "path_mae_pct": path_mae,
        "hit_candle": None,
        "directional_close_return_at_hit_pct": None,
        "available_future_candles": len(future_candles),
    }


def _measurement_for_signal(
    signal: dict[str, Any],
    *,
    candles: list[dict[str, Any]],
    ts_to_i: dict[str, int],
) -> dict[str, Any]:
    cohort = str(signal.get("cohort"))
    basis = PRIMARY_BASIS_BY_COHORT.get(cohort)
    if basis == "momentum_candle":
        measure_ts = signal.get("momentum_confirmation_timestamp")
    else:
        measure_ts = signal.get("pa_structure_break_timestamp")
    if not measure_ts:
        return {"ok": False, "reason": "MISSING_MEASUREMENT_TIMESTAMP"}
    key = _ts_str(measure_ts)
    if key not in ts_to_i:
        return {"ok": False, "reason": "MEASUREMENT_CANDLE_NOT_IN_FRAME"}
    i0 = ts_to_i[key]
    measure = candles[i0]
    if not ohlc_valid(measure):
        return {"ok": False, "reason": "INVALID_MEASUREMENT_OHLC"}
    return {
        "ok": True,
        "basis": basis,
        "measure_ts": key,
        "index": i0,
        "reference_close": float(measure["close"]),
        "future": candles[i0 + 1 :],
    }


def evaluate_confirmed_paths(
    signals: list[dict[str, Any]],
    *,
    candles: list[dict[str, Any]],
    ts_to_i: dict[str, int],
    candles_1m: pd.DataFrame | None,
    tp_pct: float = PRIMARY_TP_PCT,
    focus_horizon: int = FOCUS_HORIZON,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signal in signals:
        groups = signal_groups(signal)
        measure = _measurement_for_signal(signal, candles=candles, ts_to_i=ts_to_i)
        base = {
            "setup_id": signal.get("setup_id"),
            "side": signal.get("side"),
            "pattern_type": signal.get("pattern_type"),
            "cohort": signal.get("cohort"),
            "momentum_confidence": signal.get("momentum_confidence"),
            "confirmation_age": signal.get("confirmation_age"),
            "momentum_terminal_event": signal.get("momentum_terminal_event"),
            "regime_5m": signal.get("regime_5m"),
            "regime_15m": signal.get("regime_15m"),
            "regime_30m": signal.get("regime_30m"),
            "combined_regime": signal.get("combined_regime"),
            "groups": groups,
            "tp_pct": tp_pct,
            "focus_horizon": focus_horizon,
        }
        if not measure.get("ok"):
            rows.append(
                {
                    **base,
                    "evaluable": False,
                    "reason": measure.get("reason"),
                    "tp_hit_12": False,
                    "same_candle_ambiguous": False,
                    "intrabar_resolution": RESOLUTION_MISSING_1M,
                }
            )
            continue

        path12 = compute_signal_tp_path(
            side=str(signal["side"]),
            reference_close=float(measure["reference_close"]),
            future_candles=measure["future"],
            horizon=focus_horizon,
            tp_pct=tp_pct,
        )
        # Forward MFE/MAE/close at 12/24/48
        fwd: dict[str, Any] = {}
        for h in (12, 24, 48):
            m = compute_forward_path_metrics(
                side=str(signal["side"]),
                reference_close=float(measure["reference_close"]),
                future_candles=measure["future"],
                horizon=h,
            )
            fwd[f"mfe_{h}"] = m.get("mfe_pct")
            fwd[f"mae_{h}"] = m.get("mae_pct")
            fwd[f"dret_{h}"] = m.get("directional_close_return_pct")
            fwd[f"evaluable_{h}"] = m.get("evaluable")

        later: dict[str, Any] = {
            "tp_hit_24": False,
            "tp_hit_48": False,
            "first_later_hit_age_24": None,
            "first_later_hit_age_48": None,
        }
        if path12.get("evaluable") and not path12.get("tp_hit"):
            for h, key_hit, key_age in (
                (24, "tp_hit_24", "first_later_hit_age_24"),
                (48, "tp_hit_48", "first_later_hit_age_48"),
            ):
                p = compute_signal_tp_path(
                    side=str(signal["side"]),
                    reference_close=float(measure["reference_close"]),
                    future_candles=measure["future"],
                    horizon=h,
                    tp_pct=tp_pct,
                )
                later[key_hit] = bool(p.get("tp_hit"))
                later[key_age] = p.get("first_hit_age")

        resolution = RESOLUTION_NOT_AMBIGUOUS
        if path12.get("tp_hit") and path12.get("same_candle_ambiguous"):
            hit_c = path12.get("hit_candle") or {}
            res = resolve_same_candle_with_1m(
                side=str(signal["side"]),
                reference_close=float(measure["reference_close"]),
                tp_pct=tp_pct,
                hit_candle_5m=hit_c,
                candles_1m=candles_1m,
            )
            resolution = res["resolution"]

        # Drawdown metric for bucketing:
        # hits → mae_before_tp; non-hits → path_mae over 12
        if path12.get("tp_hit"):
            dd = path12.get("mae_before_tp_pct")
            dd_incl = path12.get("mae_including_tp_candle_pct")
        else:
            dd = path12.get("path_mae_pct")
            dd_incl = path12.get("path_mae_pct")

        rows.append(
            {
                **base,
                "measurement_basis": measure.get("basis"),
                "measurement_timestamp": measure.get("measure_ts"),
                "reference_close": measure.get("reference_close"),
                "evaluable": path12.get("evaluable"),
                "reason": path12.get("reason"),
                "tp_hit_12": bool(path12.get("tp_hit")),
                "first_hit_age": path12.get("first_hit_age"),
                "same_candle_ambiguous": bool(path12.get("same_candle_ambiguous")),
                "mae_before_tp_pct": path12.get("mae_before_tp_pct"),
                "mae_including_tp_candle_pct": path12.get("mae_including_tp_candle_pct"),
                "path_mae_12_pct": path12.get("path_mae_pct"),
                "drawdown_metric_pct": dd,
                "drawdown_including_tp_pct": dd_incl,
                "drawdown_bucket": drawdown_bucket(dd if isinstance(dd, (int, float)) else None),
                "intrabar_resolution": resolution,
                "hit_candle_timestamp": (path12.get("hit_candle") or {}).get("timestamp"),
                "directional_close_return_at_hit_pct": path12.get(
                    "directional_close_return_at_hit_pct"
                ),
                **fwd,
                **later,
            }
        )
    return rows


def build_drawdown_bucket_summary(path_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    confirmed = [
        r
        for r in path_rows
        if r.get("cohort") == COHORT_MOMENTUM_CONFIRMED and r.get("evaluable")
    ]
    n = len(confirmed)
    out: list[dict[str, Any]] = []
    for label, _lo, _hi in DRAWDOWN_BUCKETS:
        bucket_rows = [r for r in confirmed if r.get("drawdown_bucket") == label]
        hits = [r for r in bucket_rows if r.get("tp_hit_12")]
        ages = sorted(
            int(r["first_hit_age"])
            for r in hits
            if r.get("first_hit_age") is not None
        )
        out.append(
            {
                "bucket": label,
                "n": len(bucket_rows),
                "share": (len(bucket_rows) / n) if n else None,
                "tp_hit_n": len(hits),
                "tp_miss_n": len(bucket_rows) - len(hits),
                "median_hit_age": _percentile(ages, 0.5),
            }
        )
    return out


def build_non_hit_followup(path_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        r
        for r in path_rows
        if r.get("cohort") == COHORT_MOMENTUM_CONFIRMED
        and r.get("evaluable")
        and not r.get("tp_hit_12")
    ]
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "setup_id": r.get("setup_id"),
                "side": r.get("side"),
                "pattern_type": r.get("pattern_type"),
                "momentum_confidence": r.get("momentum_confidence"),
                "confirmation_age": r.get("confirmation_age"),
                "mfe_12": r.get("mfe_12"),
                "mfe_24": r.get("mfe_24"),
                "mfe_48": r.get("mfe_48"),
                "mae_12": r.get("mae_12"),
                "mae_24": r.get("mae_24"),
                "mae_48": r.get("mae_48"),
                "dret_12": r.get("dret_12"),
                "dret_24": r.get("dret_24"),
                "dret_48": r.get("dret_48"),
                "tp_hit_24": r.get("tp_hit_24"),
                "tp_hit_48": r.get("tp_hit_48"),
                "first_later_hit_age_24": r.get("first_later_hit_age_24"),
                "first_later_hit_age_48": r.get("first_later_hit_age_48"),
                "momentum_terminal_event": r.get("momentum_terminal_event"),
                "combined_regime": r.get("combined_regime"),
                "regime_5m": r.get("regime_5m"),
                "regime_15m": r.get("regime_15m"),
                "regime_30m": r.get("regime_30m"),
                "path_mae_12_pct": r.get("path_mae_12_pct"),
            }
        )
    return out


def build_same_candle_resolution(path_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "setup_id": r.get("setup_id"),
            "side": r.get("side"),
            "pattern_type": r.get("pattern_type"),
            "momentum_confidence": r.get("momentum_confidence"),
            "confirmation_age": r.get("confirmation_age"),
            "first_hit_age": r.get("first_hit_age"),
            "hit_candle_timestamp": r.get("hit_candle_timestamp"),
            "mae_before_tp_pct": r.get("mae_before_tp_pct"),
            "mae_including_tp_candle_pct": r.get("mae_including_tp_candle_pct"),
            "intrabar_resolution": r.get("intrabar_resolution"),
        }
        for r in path_rows
        if r.get("cohort") == COHORT_MOMENTUM_CONFIRMED
        and r.get("same_candle_ambiguous")
    ]


def _group_filter(rows: list[dict[str, Any]], group: str) -> list[dict[str, Any]]:
    return [r for r in rows if group in (r.get("groups") or [])]


def compute_execution_rates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluable = [r for r in rows if r.get("evaluable")]
    n = len(evaluable)
    if n == 0:
        return {
            "n_total": len(rows),
            "n_evaluable": 0,
            "optimistic_hits": 0,
            "conservative_hits": 0,
            "resolved_n": 0,
            "optimistic_hit_rate": None,
            "conservative_hit_rate": None,
            "resolved_only_hit_rate": None,
            "tp_first": 0,
            "adverse_first": 0,
            "unresolved_1m": 0,
            "missing_1m_data": 0,
            "ambiguous_n": 0,
        }

    optimistic_hits = 0
    conservative_hits = 0
    resolved_hits = 0
    resolved_n = 0
    counts = {
        RESOLUTION_TP_FIRST: 0,
        RESOLUTION_ADVERSE_FIRST: 0,
        RESOLUTION_UNRESOLVED_1M: 0,
        RESOLUTION_MISSING_1M: 0,
    }
    ambiguous_n = 0

    for r in evaluable:
        hit = bool(r.get("tp_hit_12"))
        amb = bool(r.get("same_candle_ambiguous"))
        res = r.get("intrabar_resolution") or RESOLUTION_NOT_AMBIGUOUS

        if hit and not amb:
            optimistic_hits += 1
            conservative_hits += 1
            resolved_hits += 1
            resolved_n += 1
            continue

        if hit and amb:
            ambiguous_n += 1
            if res in counts:
                counts[res] += 1
            # optimistic: count as hit
            optimistic_hits += 1
            if res == RESOLUTION_TP_FIRST:
                conservative_hits += 1
                resolved_hits += 1
                resolved_n += 1
            elif res == RESOLUTION_ADVERSE_FIRST:
                # not a conservative hit; still resolved
                resolved_n += 1
            else:
                # unresolved / missing → exclude from resolved_only denominator
                pass
            continue

        # no hit
        resolved_n += 1

    return {
        "n_total": len(rows),
        "n_evaluable": n,
        "optimistic_hits": optimistic_hits,
        "conservative_hits": conservative_hits,
        "resolved_n": resolved_n,
        "optimistic_hit_rate": optimistic_hits / n,
        "conservative_hit_rate": conservative_hits / n,
        "resolved_only_hit_rate": (resolved_hits / resolved_n) if resolved_n else None,
        "tp_first": counts[RESOLUTION_TP_FIRST],
        "adverse_first": counts[RESOLUTION_ADVERSE_FIRST],
        "unresolved_1m": counts[RESOLUTION_UNRESOLVED_1M],
        "missing_1m_data": counts[RESOLUTION_MISSING_1M],
        "ambiguous_n": ambiguous_n,
    }


def build_group_execution_summary(path_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for group in GROUP_ORDER:
        rates = compute_execution_rates(_group_filter(path_rows, group))
        out.append({"group": group, **rates})
    return out


def summarize_drawdowns(path_rows: list[dict[str, Any]]) -> dict[str, Any]:
    confirmed = [
        r
        for r in path_rows
        if r.get("cohort") == COHORT_MOMENTUM_CONFIRMED and r.get("evaluable")
    ]
    hits = [r for r in confirmed if r.get("tp_hit_12")]
    misses = [r for r in confirmed if not r.get("tp_hit_12")]

    def _stats(vals: list[float]) -> dict[str, Any]:
        s = sorted(vals)
        return {
            "n": len(s),
            "max": s[-1] if s else None,
            "median": _percentile(s, 0.5),
            "p75": _percentile(s, 0.75),
            "p90": _percentile(s, 0.90),
        }

    before = [float(r["mae_before_tp_pct"]) for r in hits if r.get("mae_before_tp_pct") is not None]
    incl = [
        float(r["mae_including_tp_candle_pct"])
        for r in hits
        if r.get("mae_including_tp_candle_pct") is not None
    ]
    miss_mae = [float(r["path_mae_12_pct"]) for r in misses if r.get("path_mae_12_pct") is not None]
    return {
        "mae_before_tp_on_hits": _stats(before),
        "mae_including_tp_candle_on_hits": _stats(incl),
        "path_mae_on_non_hits_12": _stats(miss_mae),
    }


def build_path_audit_summary(
    *,
    path_rows: list[dict[str, Any]],
    drawdown_buckets: list[dict[str, Any]],
    non_hits: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    group_exec: list[dict[str, Any]],
    drawdown_stats: dict[str, Any],
    one_m_status: str,
) -> dict[str, Any]:
    conf_exec = next(
        (g for g in group_exec if g["group"] == GROUP_MOMENTUM_CONFIRMED), None
    )
    later_24 = sum(1 for r in non_hits if r.get("tp_hit_24"))
    later_48 = sum(1 for r in non_hits if r.get("tp_hit_48"))
    adverse_hard = sum(
        1
        for r in non_hits
        if (r.get("mae_12") is not None and float(r["mae_12"]) >= 0.75)
        or (r.get("dret_12") is not None and float(r["dret_12"]) <= -0.25)
    )

    # common traits among non-hits
    traits: dict[str, Any] = {}
    if non_hits:
        from collections import Counter

        traits = {
            "sides": dict(Counter(r.get("side") for r in non_hits)),
            "patterns": dict(Counter(r.get("pattern_type") for r in non_hits)),
            "confidence": dict(Counter(r.get("momentum_confidence") for r in non_hits)),
            "confirmation_age": dict(Counter(r.get("confirmation_age") for r in non_hits)),
            "regimes": dict(Counter(r.get("combined_regime") for r in non_hits)),
        }

    res_counts = {
        RESOLUTION_TP_FIRST: sum(
            1 for r in resolutions if r.get("intrabar_resolution") == RESOLUTION_TP_FIRST
        ),
        RESOLUTION_ADVERSE_FIRST: sum(
            1
            for r in resolutions
            if r.get("intrabar_resolution") == RESOLUTION_ADVERSE_FIRST
        ),
        RESOLUTION_UNRESOLVED_1M: sum(
            1
            for r in resolutions
            if r.get("intrabar_resolution") == RESOLUTION_UNRESOLVED_1M
        ),
        RESOLUTION_MISSING_1M: sum(
            1
            for r in resolutions
            if r.get("intrabar_resolution") == RESOLUTION_MISSING_1M
        ),
    }

    answers = {
        "q_non_hit_later_tp": {
            "n_non_hits_12": len(non_hits),
            "hit_by_24": later_24,
            "hit_by_48": later_48,
        },
        "q_non_hit_adverse": {
            "n_clearly_adverse": adverse_hard,
            "note": "mae_12>=0.75% or dret_12<=-0.25%",
        },
        "q_non_hit_traits": traits,
        "q_1m_resolution": res_counts,
        "q_one_m_data_status": one_m_status,
        "q_execution_rates_confirmed": conf_exec,
    }

    return {
        "primary_tp_pct": PRIMARY_TP_PCT,
        "focus_horizon": FOCUS_HORIZON,
        "one_m_data_status": one_m_status,
        "n_confirmed_evaluable": sum(
            1
            for r in path_rows
            if r.get("cohort") == COHORT_MOMENTUM_CONFIRMED and r.get("evaluable")
        ),
        "drawdown_stats": drawdown_stats,
        "drawdown_buckets": drawdown_buckets,
        "non_hit_followup_summary": answers["q_non_hit_later_tp"],
        "same_candle_resolution_counts": res_counts,
        "group_execution_summary": group_exec,
        "research_answers": answers,
    }


def format_path_readme(summary: dict[str, Any]) -> str:
    a = summary.get("research_answers") or {}
    dd = summary.get("drawdown_stats") or {}
    conf = a.get("q_execution_rates_confirmed") or {}
    lines = [
        "# Momentum TP Path / Drawdown / Intrabar Audit (March week)",
        "",
        f"Primary TP **{summary.get('primary_tp_pct')}%**, focus horizon "
        f"**{summary.get('focus_horizon')}**. 1m data: `{summary.get('one_m_data_status')}`.",
        "",
        "## Drawdown before TP (confirmed hits)",
        "",
        f"- MAE before TP: `{dd.get('mae_before_tp_on_hits')}`",
        f"- MAE including TP candle: `{dd.get('mae_including_tp_candle_on_hits')}`",
        f"- Path MAE on non-hits @12: `{dd.get('path_mae_on_non_hits_12')}`",
        "",
        "## Drawdown buckets",
        "",
        f"```json\n{json.dumps(summary.get('drawdown_buckets'), indent=2)}\n```",
        "",
        "## Non-hits (no 0.25% within 12)",
        "",
        f"- {a.get('q_non_hit_later_tp')}",
        f"- Adverse: {a.get('q_non_hit_adverse')}",
        f"- Traits: `{a.get('q_non_hit_traits')}`",
        "",
        "## Same-candle 1m resolution",
        "",
        f"- Counts: `{summary.get('same_candle_resolution_counts')}`",
        "",
        "## Execution rates (confirmed @12)",
        "",
        f"- optimistic: `{conf.get('optimistic_hit_rate')}`",
        f"- conservative: `{conf.get('conservative_hit_rate')}`",
        f"- resolved_only: `{conf.get('resolved_only_hit_rate')}`",
        "",
        "## Group execution summary",
        "",
        f"```json\n{json.dumps(summary.get('group_execution_summary'), indent=2)}\n```",
        "",
        "## Method notes",
        "",
        "- MAE before TP excludes the TP candle; MAE including TP adds that candle's adverse.",
        "- Non-hit drawdown uses full 12-candle path MAE.",
        "- 1m resolution never invents tick order; same 1m bar touching both → unresolved_1m.",
        "- No 1m file locally → all ambiguous cases tagged missing_1m_data.",
        "",
    ]
    return "\n".join(lines)


def run_tp_path_audit(
    *,
    price_action_confirmations: list[dict[str, Any]],
    momentum_confirmations: list[dict[str, Any]],
    momentum_events: list[dict[str, Any]],
    candles: pd.DataFrame,
    symbol: str = "APTUSDT",
    tp_pct: float = PRIMARY_TP_PCT,
    focus_horizon: int = FOCUS_HORIZON,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    signals = build_signal_rows(
        price_action_confirmations=price_action_confirmations,
        momentum_confirmations=momentum_confirmations,
        momentum_events=momentum_events,
    )
    _, ts_to_i, candle_rows = _candle_maps(candles)
    candles_1m, one_m_status = load_optional_1m_candles(symbol, data_dir=data_dir)

    path_rows = evaluate_confirmed_paths(
        signals,
        candles=candle_rows,
        ts_to_i=ts_to_i,
        candles_1m=candles_1m,
        tp_pct=tp_pct,
        focus_horizon=focus_horizon,
    )
    # Also include not_confirmed in path_rows via evaluate — already all signals
    buckets = build_drawdown_bucket_summary(path_rows)
    non_hits = build_non_hit_followup(path_rows)
    resolutions = build_same_candle_resolution(path_rows)
    group_exec = build_group_execution_summary(path_rows)
    dd_stats = summarize_drawdowns(path_rows)
    summary = build_path_audit_summary(
        path_rows=path_rows,
        drawdown_buckets=buckets,
        non_hits=non_hits,
        resolutions=resolutions,
        group_exec=group_exec,
        drawdown_stats=dd_stats,
        one_m_status=one_m_status if candles_1m is not None else "missing_1m_data",
    )
    return {
        "signal_tp_paths": path_rows,
        "drawdown_bucket_summary": buckets,
        "non_hit_followup": non_hits,
        "same_candle_resolution": resolutions,
        "group_execution_summary": group_exec,
        "audit_summary": summary,
    }


def write_tp_path_outputs(
    payload: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "paths_csv": out / "signal_tp_paths.csv",
        "buckets_csv": out / "drawdown_bucket_summary.csv",
        "nonhit_csv": out / "non_hit_followup.csv",
        "resolution_csv": out / "same_candle_resolution.csv",
        "exec_csv": out / "group_execution_summary.csv",
        "summary_json": out / "audit_summary.json",
        "readme": out / "README.md",
    }
    # Flatten groups list for CSV
    path_rows = []
    for r in payload.get("signal_tp_paths") or []:
        row = dict(r)
        groups = row.pop("groups", None)
        row["groups"] = ",".join(groups) if isinstance(groups, list) else groups
        hit_c = row.pop("hit_candle", None)
        if isinstance(hit_c, dict):
            row["hit_candle_timestamp"] = row.get("hit_candle_timestamp") or hit_c.get(
                "timestamp"
            )
        path_rows.append(row)

    pd.DataFrame(json_safe(path_rows)).to_csv(paths["paths_csv"], index=False)
    pd.DataFrame(json_safe(payload.get("drawdown_bucket_summary") or [])).to_csv(
        paths["buckets_csv"], index=False
    )
    pd.DataFrame(json_safe(payload.get("non_hit_followup") or [])).to_csv(
        paths["nonhit_csv"], index=False
    )
    pd.DataFrame(json_safe(payload.get("same_candle_resolution") or [])).to_csv(
        paths["resolution_csv"], index=False
    )
    pd.DataFrame(json_safe(payload.get("group_execution_summary") or [])).to_csv(
        paths["exec_csv"], index=False
    )
    paths["summary_json"].write_text(
        json.dumps(json_safe(payload.get("audit_summary") or {}), indent=2),
        encoding="utf-8",
    )
    paths["readme"].write_text(
        format_path_readme(payload.get("audit_summary") or {}), encoding="utf-8"
    )
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Momentum TP path/drawdown/intrabar audit.")
    p.add_argument(
        "--pipeline-dir",
        default=(
            "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
        ),
    )
    p.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_momentum_tp_path_audit_march_week1",
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
    payload = run_tp_path_audit(
        price_action_confirmations=arts["price_action_confirmations"],
        momentum_confirmations=arts["momentum_confirmations"],
        momentum_events=arts["momentum_events"],
        candles=prepared["candles"],
        symbol=args.symbol,
    )
    paths = write_tp_path_outputs(payload, args.output_dir)
    summary = payload["audit_summary"]
    print(
        f"TP path audit: confirmed={summary.get('n_confirmed_evaluable')} "
        f"1m={summary.get('one_m_data_status')} "
        f"rates={((summary.get('research_answers') or {}).get('q_execution_rates_confirmed'))}"
    )
    for path in paths.values():
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
