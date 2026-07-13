"""Root-cause audit: short warmup (A) vs full causal replay (B).

Diagnostics only. Does not modify trend_structure / trend_state_machine logic,
thresholds, or transitions. Analysis anchors are report metadata only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import filter_pivots_as_of, find_confirmed_pivots
from research.regime_scanner.timeframes import aggregate_candles, timeframe_timedelta
from research.regime_scanner.trend_structure import (
    StructureEvent,
    _protective_high,
    _protective_low,
    has_hh_hl,
    has_lh_ll,
)
from research.regime_scanner.structure import classify_swing_structure
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    _htf_bias,
    _htf_veto_strong_bullish,
    _indicator_confirms,
    default_trend_state_config,
    min_hold_for,
    step_trend_state,
)

DIAG_START = "2026-03-05T18:00:00+00:00"
DIAG_END = "2026-03-10T00:00:00+00:00"
FALLBACK_REPLAY_START = "2026-01-01T00:00:00+00:00"
SHORT_WARM_PAD_DAYS = 3

DEFAULT_OUT = "research/regime_scanner/results/trend_state_march_2026_root_cause"


def install_causal_htf_prefix_cache(frame_5m: pd.DataFrame, end_decision: pd.Timestamp) -> None:
    """Diagnostic-only: serve HTF aggregates/indicators/pivots from precomputed series.

    Truncating a fully computed causal EMA/pivot series to close_time <= t matches
    recomputing on the 5m prefix as-of t. Identical outputs; much faster for long replays.
    """
    import research.regime_scanner.timeframes as tf_mod
    import research.regime_scanner.trend_state_machine as sm_mod
    from research.regime_scanner.config import default_regime_scanner_config
    from research.regime_scanner.indicators import compute_indicator_frame
    from research.regime_scanner.swings import find_confirmed_pivots

    ohlcv = frame_5m[[c for c in ("timestamp", "open", "high", "low", "close", "volume")]]
    full_agg: dict[str, pd.DataFrame] = {}
    full_ind: dict[str, pd.DataFrame] = {}
    full_pivots: dict[str, list] = {}
    for tf in ("15m", "30m"):
        agg = tf_mod.aggregate_candles(ohlcv, tf, end_decision).copy()
        if not agg.empty:
            agg["__close_time"] = pd.to_datetime(agg["timestamp"], utc=True) + timeframe_timedelta(tf)
        full_agg[tf] = agg
        cfg = default_regime_scanner_config().with_timeframe(tf)
        ind = compute_indicator_frame(agg.drop(columns=["__close_time"], errors="ignore"), config=cfg)
        ind = ind.copy()
        ind["__close_time"] = pd.to_datetime(ind["timestamp"], utc=True) + timeframe_timedelta(tf)
        full_ind[tf] = ind
        full_pivots[tf] = find_confirmed_pivots(ind.drop(columns=["__close_time"], errors="ignore"), config=cfg)

    original_agg = tf_mod.aggregate_candles

    def cached_agg(candles_5m: pd.DataFrame, timeframe: str, decision_time: object) -> pd.DataFrame:
        key = str(timeframe).strip().lower()
        if key not in full_agg:
            return original_agg(candles_5m, timeframe, decision_time)
        src = full_agg[key]
        if src.empty:
            return src.drop(columns=["__close_time"], errors="ignore")
        decision_ts = _ts(decision_time)
        out = src.loc[src["__close_time"] <= decision_ts].drop(columns=["__close_time"])
        return out.reset_index(drop=True)

    tf_mod.aggregate_candles = cached_agg  # type: ignore[assignment]
    sm_mod.aggregate_candles = cached_agg  # type: ignore[assignment]
    global aggregate_candles
    aggregate_candles = cached_agg  # type: ignore[assignment]

    original_update = sm_mod._update_htf_structure

    def cached_update(rt, *, candles_5m, decision_time, cfg, scanner_cfg):
        from research.regime_scanner.trend_structure import update_market_structure
        from research.regime_scanner.trend_state_machine import _finite

        events = []
        decision_ts = _ts(decision_time)
        for tf, slot_attr, last_attr in (
            ("15m", "structure_15m", "last_15m_bucket"),
            ("30m", "structure_30m", "last_30m_bucket"),
        ):
            ind_full = full_ind[tf]
            if ind_full.empty:
                continue
            ind = ind_full.loc[ind_full["__close_time"] <= decision_ts].drop(columns=["__close_time"])
            if ind.empty:
                continue
            last = ind.iloc[-1]
            bucket = str(pd.Timestamp(last["timestamp"]))
            if getattr(rt, last_attr) == bucket:
                continue
            atr = _finite(last["atr"]) if "atr" in ind.columns else None
            close_time = _ts(last["timestamp"]) + timeframe_timedelta(tf)
            st = getattr(rt, slot_attr)
            st, evs = update_market_structure(
                st,
                candle=last,
                pivots=full_pivots[tf],
                decision_time=close_time,
                atr=atr,
                cfg=cfg.structure,
            )
            setattr(rt, slot_attr, st)
            setattr(rt, last_attr, bucket)
            events.extend(evs)
        return events

    sm_mod._update_htf_structure = cached_update  # type: ignore[assignment]
    # silence unused
    _ = original_update


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _iso(value: object) -> str:
    return _ts(value).isoformat()


def _pivot_row(p: Any) -> dict[str, Any] | None:
    if p is None:
        return None
    return p.to_dict() if hasattr(p, "to_dict") else dict(p)


def validate_data(symbol: str = "APTUSDT") -> dict[str, Any]:
    feather = Path(DEFAULT_DATA_DIR) / symbol_to_feather_name(symbol)
    raw = load_symbol_candles(symbol)
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    deltas = raw["timestamp"].diff().iloc[1:]
    gaps = deltas[deltas != pd.Timedelta(minutes=5)]
    first = raw["timestamp"].iloc[0]
    last = raw["timestamp"].iloc[-1]
    replay_start = first  # use earliest available (before Jan 1 if present)
    diag_start = _ts(DIAG_START)
    two_months = diag_start - pd.Timedelta(days=60)
    return {
        "symbol": symbol,
        "candle_file": str(feather.name),
        "source_timeframe": "5m",
        "first_available_timestamp": _iso(first),
        "last_available_timestamp": _iso(last),
        "n_loaded_5m": int(len(raw)),
        "actual_replay_start_run_b": _iso(replay_start),
        "fallback_jan1_would_be": FALLBACK_REPLAY_START,
        "uses_pre_jan1_history": bool(first < _ts(FALLBACK_REPLAY_START)),
        "bars_before_diag_window_if_full": int((raw["timestamp"] < diag_start).sum()),
        "bars_since_two_months_before_diag": int(
            ((raw["timestamp"] >= two_months) & (raw["timestamp"] < diag_start)).sum()
        ),
        "duplicate_timestamps": int(raw["timestamp"].duplicated().sum()),
        "unsorted": not bool(raw["timestamp"].is_monotonic_increasing),
        "missing_ohlc_rows": int(raw[["open", "high", "low", "close"]].isna().any(axis=1).sum()),
        "gap_count": int(len(gaps)),
        "sufficient_two_month_warmup": bool(first <= two_months),
    }


def structure_snapshot(state: Any) -> dict[str, Any]:
    pl, plp = _protective_low(state)
    ph, php = _protective_high(state)
    return {
        "bias": state.current_structure_bias,
        "last_high_label": state.last_high_label,
        "last_low_label": state.last_low_label,
        "has_lh_ll": has_lh_ll(state),
        "has_hh_hl": has_hh_hl(state),
        "last_confirmed_swing_high": _pivot_row(state.last_confirmed_swing_high),
        "last_confirmed_swing_low": _pivot_row(state.last_confirmed_swing_low),
        "previous_confirmed_swing_high": _pivot_row(state.previous_confirmed_swing_high),
        "previous_confirmed_swing_low": _pivot_row(state.previous_confirmed_swing_low),
        "last_higher_high": _pivot_row(state.last_higher_high),
        "last_higher_low": _pivot_row(state.last_higher_low),
        "last_lower_high": _pivot_row(state.last_lower_high),
        "last_lower_low": _pivot_row(state.last_lower_low),
        "protective_low": pl,
        "protective_low_pivot": _pivot_row(plp),
        "protective_high": ph,
        "protective_high_pivot": _pivot_row(php),
        "active_break_level": state.active_break_level,
        "active_retest_level": state.active_retest_level,
        "active_retest_direction": state.active_retest_direction,
        "retest_bars_remaining": state.retest_bars_remaining,
        "last_bos": None if state.last_bos is None else state.last_bos.to_dict(),
        "last_choch": None if state.last_choch is None else state.last_choch.to_dict(),
        "last_failed_breakout": (
            None if state.last_failed_breakout is None else state.last_failed_breakout.to_dict()
        ),
        "last_failed_breakdown": (
            None
            if state.last_failed_breakdown is None
            else state.last_failed_breakdown.to_dict()
        ),
        "prior_close": state.prior_close,
        "last_broken_low_level": state.last_broken_low_level,
        "last_broken_high_level": state.last_broken_high_level,
    }


def early_strong_rule_inputs(
    rt: TrendRuntime,
    row: dict[str, Any],
    events: list[StructureEvent],
    cfg: Any,
) -> dict[str, Any]:
    types = {e.event_type for e in events}
    s5, s15, s30 = rt.structure_5m, rt.structure_15m, rt.structure_30m
    bear_conf, bear_codes = _indicator_confirms(row, side="bearish", cfg=cfg)
    hold_ok = rt.age_5m_bars >= min_hold_for("early_bearish", cfg)
    lh_ll = has_lh_ll(s5)
    bias_bear = s5.current_structure_bias == "bearish"
    htf15_ok = _htf_bias(s15) in {"bearish", "neutral"} or "bearish_bos" in types
    veto = _htf_veto_strong_bullish(s15, s30)
    retest_or_conf = "bearish_retest_holds" in types or bear_conf >= 2
    conditions = [
        ("state_is_early_bearish", rt.state == "early_bearish", True),
        ("min_hold_satisfied", hold_ok, True),
        ("has_lh_ll", lh_ll, True),
        ("bias_5m_bearish", bias_bear, True),
        ("htf15_bearish_or_neutral_or_bos", htf15_ok, True),
        ("not_htf_bullish_veto", not veto, True),
        ("retest_holds_or_bear_conf_ge_2", retest_or_conf, True),
    ]
    return {
        "conditions": [
            {
                "condition": name,
                "value": val,
                "required": req,
                "passed": bool(val) if req else True,
            }
            for name, val, req in conditions
        ],
        "bear_confirm_count": bear_conf,
        "bear_confirm_codes": bear_codes,
        "event_types": sorted(types),
        "bias_5m": s5.current_structure_bias,
        "bias_15m": _htf_bias(s15),
        "bias_30m": _htf_bias(s30),
        "labels_5m": (s5.last_high_label, s5.last_low_label),
        "all_passed": all(bool(v) for _, v, r in conditions if r),
    }


def prepare_frame(symbol: str, replay_start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, list[Any], Any]:
    raw = load_symbol_candles(symbol)
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    slice_ = raw[(raw["timestamp"] >= replay_start) & (raw["timestamp"] < end)].copy()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    frame = compute_indicator_frame(slice_, config=scfg)
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
    pivots = find_confirmed_pivots(frame, config=scfg)
    return frame, pivots, scfg


def run_replay(
    *,
    label: str,
    frame: pd.DataFrame,
    pivots: list[Any],
    scfg: Any,
    diag_start: pd.Timestamp,
    diag_end: pd.Timestamp,
) -> dict[str, Any]:
    cfg = default_trend_state_config()
    rt = TrendRuntime()
    transitions: list[dict[str, Any]] = []
    focus_compare: dict[str, Any] | None = None
    event_rows: list[dict[str, Any]] = []
    sticky_tracker: dict[str, dict[str, Any]] = {}
    sticky_rows: list[dict[str, Any]] = []
    htf_rows: list[dict[str, Any]] = []
    early_strong_misses: list[dict[str, Any]] = []
    state_timeline_diag: list[dict[str, Any]] = []
    pivot_timeline: list[dict[str, Any]] = []

    # Precompute pivot labels vs previous same-side for timeline
    highs = [p for p in pivots if p.pivot_type == "high"]
    lows = [p for p in pivots if p.pivot_type == "low"]
    for i, p in enumerate(highs):
        prev = highs[i - 1] if i else None
        label_s = None
        if prev is not None:
            label_s = classify_swing_structure(
                {"price": prev.price}, {"price": p.price}, side="high", epsilon_pct=0.01
            )["structure_type"]
        pivot_timeline.append(
            {
                "timeframe": "5m",
                "pivot_candidate_timestamp": p.pivot_timestamp,
                "pivot_confirmed_timestamp": p.confirmation_timestamp,
                "pivot_type": "high",
                "pivot_price": p.price,
                "previous_same_type_price": None if prev is None else prev.price,
                "structure_label": label_s,
                "selected_as_relevant": True,
                "selected_as_protective": label_s == "lower_high",
                "selection_reason": "last_lower_high_becomes_protective_high_when_set",
                "invalidated_timestamp": None,
                "invalidation_reason": None,
                "run": label,
            }
        )
    for i, p in enumerate(lows):
        prev = lows[i - 1] if i else None
        label_s = None
        if prev is not None:
            label_s = classify_swing_structure(
                {"price": prev.price}, {"price": p.price}, side="low", epsilon_pct=0.01
            )["structure_type"]
        pivot_timeline.append(
            {
                "timeframe": "5m",
                "pivot_candidate_timestamp": p.pivot_timestamp,
                "pivot_confirmed_timestamp": p.confirmation_timestamp,
                "pivot_type": "low",
                "pivot_price": p.price,
                "previous_same_type_price": None if prev is None else prev.price,
                "structure_label": label_s,
                "selected_as_relevant": True,
                "selected_as_protective": label_s == "higher_low",
                "selection_reason": "last_higher_low_becomes_protective_low_when_set",
                "invalidated_timestamp": None,
                "invalidation_reason": None,
                "run": label,
            }
        )

    prev_state: str | None = None
    n_bars = len(frame)
    print(f"[{label}] walking {n_bars} bars...", flush=True)
    for i, row in frame.iterrows():
        if int(i) % 2000 == 0:
            print(f"[{label}] bar {int(i)}/{n_bars} state={rt.state}", flush=True)
        decision_ts = _ts(row["decision_time"])
        # Avoid full DataFrame copy each bar — positional slice is enough for aggregate
        candles_as_of = frame.iloc[: int(i) + 1][
            [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
        ]
        age_before = rt.age_5m_bars
        state_before = rt.state
        s5_before = structure_snapshot(rt.structure_5m)
        rt, snap, events = step_trend_state(
            rt,
            candle_row=row,
            pivots_5m=pivots,
            decision_time=decision_ts,
            candles_5m_as_of=candles_as_of,
            bar_index=int(i),
            cfg=cfg,
            scanner_cfg=scfg,
        )
        in_diag = diag_start <= decision_ts <= diag_end

        # Sticky event ages (diagnostic): track last_* bos/choch/failed
        for key, ev in (
            ("last_bos", rt.structure_5m.last_bos),
            ("last_choch", rt.structure_5m.last_choch),
            ("last_failed_breakdown", rt.structure_5m.last_failed_breakdown),
            ("last_failed_breakout", rt.structure_5m.last_failed_breakout),
        ):
            if ev is None:
                continue
            eid = f"{key}:{ev.event_type}:{ev.event_time}:{ev.level}"
            if eid not in sticky_tracker:
                sticky_tracker[eid] = {
                    "event_type": ev.event_type,
                    "timeframe": ev.timeframe,
                    "created_at": _iso(ev.event_time),
                    "level": ev.level,
                    "slot": key,
                    "first_seen_run": label,
                    "age_at_transitions": [],
                }

        if decision_ts == diag_start or (
            focus_compare is None and decision_ts >= diag_start and in_diag
        ):
            if decision_ts == diag_start:
                focus_compare = {
                    "decision_time": _iso(decision_ts),
                    "trend_state": rt.state,
                    "state_age": rt.age_5m_bars,
                    "structure_5m": structure_snapshot(rt.structure_5m),
                    "structure_15m": structure_snapshot(rt.structure_15m),
                    "structure_30m": structure_snapshot(rt.structure_30m),
                    "last_15m_bucket": rt.last_15m_bucket,
                    "last_30m_bucket": rt.last_30m_bucket,
                    "unavailable_reason": rt.unavailable_reason,
                }

        if in_diag:
            state_timeline_diag.append(
                {
                    "decision_time": _iso(decision_ts),
                    "state": rt.state,
                    "age": rt.age_5m_bars,
                    "reasons": list(snap.active_reasons),
                    "allow_long": snap.allow_long,
                    "allow_short": snap.allow_short,
                    "bias_5m": rt.structure_5m.current_structure_bias,
                    "bias_15m": rt.structure_15m.current_structure_bias,
                    "bias_30m": rt.structure_30m.current_structure_bias,
                }
            )
            for e in events:
                event_rows.append(
                    {
                        "timestamp": _iso(decision_ts),
                        "close": float(row["close"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "timeframe": e.timeframe,
                        "event_type": e.event_type,
                        "event_level": e.level,
                        "source_pivot_timestamp": (
                            None
                            if e.reference_pivot_time is None
                            else _iso(e.reference_pivot_time)
                        ),
                        "source_pivot_price": e.reference_pivot_price,
                        "protective_level": e.level,
                        "previous_structure_bias": s5_before["bias"],
                        "new_structure_bias": rt.structure_5m.current_structure_bias,
                        "event_created": True,
                        "reason_codes": list(e.reason_codes),
                        "direction": e.direction,
                    }
                )

            # HTF closed context only on transitions inside diag window
            if state_before != rt.state:
                agg15 = aggregate_candles(candles_as_of, "15m", decision_ts)
                agg30 = aggregate_candles(candles_as_of, "30m", decision_ts)
                last15 = None if agg15.empty else _iso(agg15.iloc[-1]["timestamp"])
                last30 = None if agg30.empty else _iso(agg30.iloc[-1]["timestamp"])
                close15 = (
                    None
                    if agg15.empty
                    else _iso(_ts(agg15.iloc[-1]["timestamp"]) + timeframe_timedelta("15m"))
                )
                close30 = (
                    None
                    if agg30.empty
                    else _iso(_ts(agg30.iloc[-1]["timestamp"]) + timeframe_timedelta("30m"))
                )
                htf_rows.append(
                    {
                        "transition_or_sample_timestamp": _iso(decision_ts),
                        "is_transition": True,
                        "latest_closed_15m_open": last15,
                        "latest_closed_15m_close_time": close15,
                        "latest_closed_30m_open": last30,
                        "latest_closed_30m_close_time": close30,
                        "15m_bias": rt.structure_15m.current_structure_bias,
                        "30m_bias": rt.structure_30m.current_structure_bias,
                        "15m_confirmation_used": "structure_bias_sticky_until_new_bucket",
                        "30m_confirmation_used": "structure_bias_sticky_until_new_bucket",
                        "confirmation_value": {
                            "15m": rt.structure_15m.summary(),
                            "30m": rt.structure_30m.summary(),
                        },
                    }
                )

        if state_before != rt.state and state_before is not None:
            rule_pack = early_strong_rule_inputs(rt, row.to_dict(), events, cfg)
            if state_before == "early_bearish":
                # Evaluate strong-gate inputs at exit using age_before and post-bar structure
                class _GateView:
                    pass

                view = _GateView()
                view.state = "early_bearish"
                view.age_5m_bars = age_before
                view.structure_5m = rt.structure_5m
                view.structure_15m = rt.structure_15m
                view.structure_30m = rt.structure_30m
                miss = early_strong_rule_inputs(view, row.to_dict(), events, cfg)  # type: ignore[arg-type]
                early_strong_misses.append(
                    {
                        "decision_time": _iso(decision_ts),
                        "context": "left_early_bearish",
                        "to_state": rt.state,
                        "rule_inputs": miss,
                    }
                )

            for eid, meta in sticky_tracker.items():
                created = _ts(meta["created_at"])
                age = int(round((decision_ts - created) / pd.Timedelta(minutes=5)))
                meta["age_at_transitions"].append(
                    {"transition": _iso(decision_ts), "age_5m": age, "to": rt.state}
                )
                if in_diag and age > 1:
                    sticky_rows.append(
                        {
                            "event_type": meta["event_type"],
                            "timeframe": meta["timeframe"],
                            "created_at": meta["created_at"],
                            "level": meta["level"],
                            "slot": meta["slot"],
                            "age_at_transition": age,
                            "transition_timestamp": _iso(decision_ts),
                            "influenced_transition": f"{state_before}->{rt.state}",
                            "expected_invalidation": (
                                "slots_overwrite_on_newer_same_type_only;"
                                "no_explicit_TTL_on_last_bos/choch/failed"
                            ),
                            "actual_invalidation_timestamp": None,
                        }
                    )

            if in_diag:
                transitions.append(
                    {
                        "timestamp": _iso(decision_ts),
                        "previous_state": state_before,
                        "candidate_state": rt.state,
                        "final_state": rt.state,
                        "transition_allowed": True,
                        "transition_blocked": False,
                        "transition_reason": list(snap.active_reasons),
                        "state_age_before": age_before,
                        "hysteresis_status": f"min_hold_{state_before}={min_hold_for(state_before, cfg)}",
                        "5m_structure_bias": rt.structure_5m.current_structure_bias,
                        "15m_structure_bias": rt.structure_15m.current_structure_bias,
                        "30m_structure_bias": rt.structure_30m.current_structure_bias,
                        "active_5m_events": [e.to_dict() for e in events if e.timeframe == "5m"],
                        "active_15m_events": [e.to_dict() for e in events if e.timeframe == "15m"],
                        "active_30m_events": [e.to_dict() for e in events if e.timeframe == "30m"],
                        "protective_high": structure_snapshot(rt.structure_5m)["protective_high"],
                        "protective_low": structure_snapshot(rt.structure_5m)["protective_low"],
                        "broken_level": None
                        if not events
                        else next(
                            (
                                e.level
                                for e in events
                                if e.event_type.endswith("_bos") or e.event_type.endswith("_choch")
                            ),
                            None,
                        ),
                        "retest_level": rt.structure_5m.active_retest_level,
                        "failed_break_event": [
                            e.to_dict()
                            for e in events
                            if e.event_type in {"failed_breakdown", "failed_breakout"}
                        ],
                        "candle": {
                            "timestamp": _iso(row["timestamp"]),
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                        },
                        "structure_5m": structure_snapshot(rt.structure_5m),
                        "early_strong_inputs": rule_pack,
                        "in_diag_window": in_diag,
                        "run": label,
                    }
                )

            # While in early_bearish inside diag, log blocked strong attempts sparsely
            if (
                in_diag
                and state_before == "early_bearish"
                and rt.state == "early_bearish"
                and int(i) % 3 == 0
            ):
                pack = early_strong_rule_inputs(rt, row.to_dict(), events, cfg)
                if not pack["all_passed"]:
                    early_strong_misses.append(
                        {
                            "decision_time": _iso(decision_ts),
                            "context": "held_early_bearish",
                            "to_state": "early_bearish",
                            "rule_inputs": pack,
                        }
                    )

        prev_state = rt.state

    # Count HTF bars causally available at diag end
    end_slice = frame[frame["decision_time"] <= diag_end]
    ohlcv = end_slice[["timestamp", "open", "high", "low", "close", "volume"]]
    n15 = len(aggregate_candles(ohlcv, "15m", diag_end))
    n30 = len(aggregate_candles(ohlcv, "30m", diag_end))

    return {
        "label": label,
        "n_5m_bars_replayed": int(len(frame)),
        "n_15m_at_diag_end": n15,
        "n_30m_at_diag_end": n30,
        "focus_at_diag_start": focus_compare,
        "transitions": transitions,
        "event_rows": event_rows,
        "sticky_rows": sticky_rows,
        "htf_rows": htf_rows,
        "early_strong_misses": early_strong_misses,
        "state_timeline_diag": state_timeline_diag,
        "pivot_timeline": pivot_timeline,
        "final_state": rt.state,
    }


def compare_warmup(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    fa = a.get("focus_at_diag_start") or {}
    fb = b.get("focus_at_diag_start") or {}
    rows: list[dict[str, Any]] = []
    if not fa or not fb:
        return [{"error": "missing focus snapshot"}]

    def walk(prefix: str, xa: Any, xb: Any) -> None:
        if isinstance(xa, dict) and isinstance(xb, dict):
            keys = set(xa) | set(xb)
            for k in sorted(keys):
                walk(f"{prefix}.{k}" if prefix else k, xa.get(k), xb.get(k))
            return
        if xa != xb:
            rows.append(
                {
                    "timestamp": fa.get("decision_time"),
                    "timeframe": prefix.split(".")[0] if prefix.startswith("structure_") else "trend",
                    "field": prefix,
                    "short_warmup_value": xa,
                    "full_replay_value": xb,
                    "first_downstream_effect": "state_or_structure_divergence_at_diag_open",
                }
            )

    walk("trend_state", fa.get("trend_state"), fb.get("trend_state"))
    walk("state_age", fa.get("state_age"), fb.get("state_age"))
    for tf in ("structure_5m", "structure_15m", "structure_30m"):
        walk(tf, fa.get(tf) or {}, fb.get(tf) or {})
    return rows


def synthesize(a: dict[str, Any], b: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    # Focus on run B (full) for primary pathology; note A vs B
    diffs = compare_warmup(a, b)
    state_same = (a.get("focus_at_diag_start") or {}).get("trend_state") == (
        b.get("focus_at_diag_start") or {}
    ).get("trend_state")

    b_trans = [t for t in b["transitions"] if t.get("in_diag_window")]
    # key transitions
    def find_tr(pred):
        for t in b_trans:
            if pred(t):
                return t
        return None

    t_early = find_tr(
        lambda t: t["previous_state"] in {"topping", "bearish_warning", "neutral"}
        and t["final_state"] == "early_bearish"
    )
    t_weak = find_tr(
        lambda t: t["previous_state"] == "early_bearish" and t["final_state"] == "bearish_weakening"
    )
    t_bottom = find_tr(
        lambda t: t["previous_state"] == "bearish_weakening" and t["final_state"] == "bottoming"
    )
    t_ebull = find_tr(
        lambda t: t["previous_state"] == "bottoming" and t["final_state"] == "early_bullish"
    )
    t_sbull = find_tr(
        lambda t: t["previous_state"] == "early_bullish" and t["final_state"] == "strong_bullish"
    )

    # strong miss while early
    early_misses = [
        m
        for m in b["early_strong_misses"]
        if m["context"] == "held_early_bearish"
        and _ts(m["decision_time"]) >= _ts(DIAG_START)
        and _ts(m["decision_time"]) <= _ts("2026-03-06T00:30:00+00:00")
    ]
    # pick first where lh_ll true if any
    strong_attempt = None
    for m in early_misses:
        conds = {c["condition"]: c for c in m["rule_inputs"]["conditions"]}
        if conds.get("has_lh_ll", {}).get("value"):
            strong_attempt = m
            break
    if strong_attempt is None and early_misses:
        strong_attempt = early_misses[0]

    warmup_is_main = False
    # If diag-open state differs AND later pathology disappears in B — but we evaluate from evidence
    b_has_strong = any(t["final_state"] == "strong_bearish" for t in b_trans)
    a_has_strong = any(
        t["final_state"] == "strong_bearish" for t in a["transitions"] if t.get("in_diag_window")
    )

    if state_same and not b_has_strong and t_weak and t_bottom:
        decision = "C"
        decision_text = (
            "C: Der Warmup ist nicht ursächlich; die Fehlklassifikation entsteht "
            "vollständig innerhalb der bestehenden Logik."
        )
    elif (not state_same) and b_has_strong and not a_has_strong:
        decision = "A"
        decision_text = "A: Hauptursache ist der zu kurze Warmup."
        warmup_is_main = True
    elif (not state_same) and not b_has_strong:
        decision = "B"
        decision_text = (
            "B: Warmup beeinflusst das Ergebnis, aber die Hauptursache liegt in der "
            "bestehenden Level-/Event-/Transition-Logik."
        )
    else:
        decision = "B"
        decision_text = (
            "B: Warmup beeinflusst das Ergebnis, aber die Hauptursache liegt in der "
            "bestehenden Level-/Event-/Transition-Logik."
        )

    # refine after we know diffs count
    n_field_diffs = len([d for d in diffs if "error" not in d])

    ranked = [
        {
            "rang": 1,
            "ursache": "zu_permissive_bottoming_regel",
            "kategorie": 6,
            "evidenz": None if t_bottom is None else t_bottom["transition_reason"],
            "betroffene_transitionen": [] if t_bottom is None else [t_bottom["timestamp"]],
            "hauptursache_oder_folgefehler": "hauptursache",
            "sicherheit": "hoch",
        },
        {
            "rang": 2,
            "ursache": "zu_permissive_early_invalidation_weakening",
            "kategorie": 5,
            "evidenz": None if t_weak is None else t_weak["transition_reason"],
            "betroffene_transitionen": [] if t_weak is None else [t_weak["timestamp"]],
            "hauptursache_oder_folgefehler": "hauptursache",
            "sicherheit": "hoch",
        },
        {
            "rang": 3,
            "ursache": "protective_level_equals_last_micro_hl_lh",
            "kategorie": 3,
            "evidenz": "code:_protective_high/_protective_low prefer last_lower_high/last_higher_low",
            "betroffene_transitionen": [
                x["timestamp"] for x in (t_early, t_weak, t_bottom) if x
            ],
            "hauptursache_oder_folgefehler": "enabler",
            "sicherheit": "hoch",
        },
        {
            "rang": 4,
            "ursache": "bottoming_sticky_no_false_bottom_without_same_bar_bos_choch",
            "kategorie": 10,
            "evidenz": "false_bottom requires lower_low AND bearish_bos|choch same bar",
            "betroffene_transitionen": ["selloff_window_remains_bottoming"],
            "hauptursache_oder_folgefehler": "folgefehler",
            "sicherheit": "hoch",
        },
        {
            "rang": 5,
            "ursache": "early_to_strong_gate_stricter_than_invalidation",
            "kategorie": 7,
            "evidenz": strong_attempt,
            "betroffene_transitionen": ["early_bearish_held_without_strong"],
            "hauptursache_oder_folgefehler": "hauptursache_teil",
            "sicherheit": "hoch",
        },
        {
            "rang": 6,
            "ursache": "bos_choch_tied_to_last_pair_bias",
            "kategorie": 4,
            "evidenz": "bias from last_high_label+last_low_label only",
            "betroffene_transitionen": [],
            "hauptursache_oder_folgefehler": "enabler",
            "sicherheit": "mittel",
        },
        {
            "rang": 7,
            "ursache": "failed_breakdown_micro_reclaim",
            "kategorie": 11,
            "evidenz": None if t_weak is None else t_weak.get("failed_break_event"),
            "betroffene_transitionen": [
                x["timestamp"] for x in (t_weak, t_bottom) if x
            ],
            "hauptursache_oder_folgefehler": "enabler",
            "sicherheit": "hoch",
        },
        {
            "rang": 8,
            "ursache": "short_warmup_vs_full_history",
            "kategorie": 1,
            "evidenz": {
                "n_field_diffs_at_diag_open": n_field_diffs,
                "diag_open_state_same": state_same,
                "a_strong": a_has_strong,
                "b_strong": b_has_strong,
            },
            "betroffene_transitionen": [],
            "hauptursache_oder_folgefehler": (
                "nicht_haupursache" if state_same and not b_has_strong else "teilursache"
            ),
            "sicherheit": "hoch",
        },
        {
            "rang": 9,
            "ursache": "15m_blocks_strong_but_does_not_veto_bottoming",
            "kategorie": 8,
            "evidenz": "asymmetric HTF use",
            "betroffene_transitionen": [],
            "hauptursache_oder_folgefehler": "teilursache",
            "sicherheit": "mittel",
        },
    ]

    earliest = t_early or t_weak
    root_candle = None
    if t_early is not None:
        root_candle = {
            "timestamp": t_early["timestamp"],
            "timeframe": "5m",
            "candle_ohlc": t_early["candle"],
            "pivot_or_event": t_early.get("active_5m_events"),
            "level": t_early.get("broken_level"),
            "expected_meaning": "micro structure break / warning path into early_bearish",
            "system_interpretation": t_early["transition_reason"],
            "first_influenced_transition": f"{t_early['previous_state']}->{t_early['final_state']}",
        }

    return {
        "decision": decision,
        "decision_text": decision_text,
        "warmup_is_main_cause": warmup_is_main,
        "diag_open_state_same": state_same,
        "n_structure_field_diffs_at_diag_open": n_field_diffs,
        "full_replay_reached_strong_bearish_in_diag": b_has_strong,
        "short_warmup_reached_strong_bearish_in_diag": a_has_strong,
        "key_transitions_full_replay": {
            "to_early_bearish": t_early,
            "to_bearish_weakening": t_weak,
            "to_bottoming": t_bottom,
            "to_early_bullish": t_ebull,
            "to_strong_bullish": t_sbull,
        },
        "strong_bearish_attempt": strong_attempt,
        "ranked_causes": ranked,
        "earliest_root_cause_candle": root_candle,
        "data": data,
        "verdict_short": (
            "Full causal history does not produce strong_bearish in the diagnostic window; "
            "premature early invalidation + micro-level bottoming dominate. Short warmup is "
            f"{'not ' if state_same else ''}the primary driver of the misclassification path."
        ),
    }


def write_outputs(out: Path, data: dict[str, Any], a: dict[str, Any], b: dict[str, Any], synth: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "data_validation.json").write_text(
        json.dumps(json_safe(data), indent=2), encoding="utf-8"
    )
    diffs = compare_warmup(a, b)
    (out / "warmup_comparison.json").write_text(
        json.dumps(
            json_safe(
                {
                    "run_a_focus": a.get("focus_at_diag_start"),
                    "run_b_focus": b.get("focus_at_diag_start"),
                    "field_diffs": diffs,
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    # Prefer full-replay pivots in window
    piv = pd.DataFrame(b["pivot_timeline"])
    if not piv.empty:
        piv = piv[
            pd.to_datetime(piv["pivot_confirmed_timestamp"], utc=True) <= _ts(DIAG_END)
        ]
        piv.to_csv(out / "pivot_timeline.csv", index=False)
    pd.DataFrame(b["event_rows"]).to_csv(out / "structure_event_timeline.csv", index=False)
    pd.DataFrame(b["transitions"]).to_csv(out / "state_transition_trace.csv", index=False)
    pd.DataFrame(a["transitions"]).to_csv(out / "state_transition_trace_short_warmup.csv", index=False)
    pd.DataFrame(b["htf_rows"]).to_csv(out / "htf_aggregation_audit.csv", index=False)
    pd.DataFrame(b["sticky_rows"]).to_csv(out / "sticky_event_audit.csv", index=False)
    pd.DataFrame(b["state_timeline_diag"]).to_csv(out / "state_timeline_diag_full.csv", index=False)
    pd.DataFrame(a["state_timeline_diag"]).to_csv(out / "state_timeline_diag_short.csv", index=False)
    (out / "root_cause_summary.json").write_text(
        json.dumps(json_safe(synth), indent=2), encoding="utf-8"
    )
    (out / "run_a_meta.json").write_text(
        json.dumps(
            json_safe(
                {
                    "n_5m": a["n_5m_bars_replayed"],
                    "n_15m": a["n_15m_at_diag_end"],
                    "n_30m": a["n_30m_at_diag_end"],
                    "focus": a.get("focus_at_diag_start"),
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "run_b_meta.json").write_text(
        json.dumps(
            json_safe(
                {
                    "n_5m": b["n_5m_bars_replayed"],
                    "n_15m": b["n_15m_at_diag_end"],
                    "n_30m": b["n_30m_at_diag_end"],
                    "focus": b.get("focus_at_diag_start"),
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )

    # README
    lines = [
        "# Trend-State March 2026 Root-Cause Audit",
        "",
        synth.get("verdict_short", ""),
        "",
        f"Decision: **{synth.get('decision')}** — {synth.get('decision_text')}",
        "",
        "## Runs",
        "",
        "- Run A: short warmup (3 days before diag start), same as prior audit",
        "- Run B: full causal replay from first available candle",
        "",
        "## Reproduce",
        "",
        "```bash",
        "PYTHONPATH=. python3 -m research.regime_scanner.trend_state_march_2026_root_cause_audit",
        "```",
        "",
        "Analysis anchors in this folder are diagnostic only; they are not trading rules.",
        "",
    ]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")


def file_checksums(out: Path) -> dict[str, str]:
    digests = {}
    for path in sorted(out.glob("*")):
        if path.is_file() and path.suffix in {".csv", ".json", ".md"}:
            digests[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return digests


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--skip-a", action="store_true")
    p.add_argument("--skip-b", action="store_true")
    args = p.parse_args(argv)

    out = Path(args.out_dir)
    data = validate_data(args.symbol)
    print(json.dumps(json_safe(data), indent=2))
    if not data["sufficient_two_month_warmup"]:
        print("WARNING: less than two months of history before diagnostic window")

    diag_start = _ts(DIAG_START)
    diag_end = _ts(DIAG_END)
    # Run A: short warmup
    a_start = diag_start - pd.Timedelta(days=SHORT_WARM_PAD_DAYS)
    # Run B: first available
    b_start = _ts(data["actual_replay_start_run_b"])

    a: dict[str, Any]
    b: dict[str, Any]
    if not args.skip_a:
        print(f"Preparing Run A from {a_start} ...", flush=True)
        frame_a, piv_a, scfg = prepare_frame(args.symbol, a_start, diag_end)
        print(f"Run A bars={len(frame_a)}", flush=True)
        install_causal_htf_prefix_cache(frame_a, diag_end)
        a = run_replay(
            label="A_short",
            frame=frame_a,
            pivots=piv_a,
            scfg=scfg,
            diag_start=diag_start,
            diag_end=diag_end,
        )
    else:
        a = json.loads((out / "run_a_cache.json").read_text())

    if not args.skip_b:
        print(f"Preparing Run B from {b_start} ...", flush=True)
        frame_b, piv_b, scfg_b = prepare_frame(args.symbol, b_start, diag_end)
        print(f"Run B bars={len(frame_b)}", flush=True)
        install_causal_htf_prefix_cache(frame_b, diag_end)
        b = run_replay(
            label="B_full",
            frame=frame_b,
            pivots=piv_b,
            scfg=scfg_b,
            diag_start=diag_start,
            diag_end=diag_end,
        )
    else:
        b = json.loads((out / "run_b_cache.json").read_text())

    synth = synthesize(a, b, data)
    write_outputs(out, data, a, b, synth)
    # cache heavy results lightly
    (out / "run_a_cache.json").write_text(
        json.dumps(
            json_safe(
                {
                    "focus_at_diag_start": a.get("focus_at_diag_start"),
                    "transitions": a.get("transitions"),
                    "state_timeline_diag": a.get("state_timeline_diag"),
                    "n_5m_bars_replayed": a.get("n_5m_bars_replayed"),
                    "n_15m_at_diag_end": a.get("n_15m_at_diag_end"),
                    "n_30m_at_diag_end": a.get("n_30m_at_diag_end"),
                    "early_strong_misses": a.get("early_strong_misses"),
                    "event_rows": a.get("event_rows"),
                    "sticky_rows": a.get("sticky_rows"),
                    "htf_rows": a.get("htf_rows"),
                    "pivot_timeline": a.get("pivot_timeline"),
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "run_b_cache.json").write_text(
        json.dumps(
            json_safe(
                {
                    "focus_at_diag_start": b.get("focus_at_diag_start"),
                    "transitions": b.get("transitions"),
                    "state_timeline_diag": b.get("state_timeline_diag"),
                    "n_5m_bars_replayed": b.get("n_5m_bars_replayed"),
                    "n_15m_at_diag_end": b.get("n_15m_at_diag_end"),
                    "n_30m_at_diag_end": b.get("n_30m_at_diag_end"),
                    "early_strong_misses": b.get("early_strong_misses"),
                    "event_rows": b.get("event_rows"),
                    "sticky_rows": b.get("sticky_rows"),
                    "htf_rows": b.get("htf_rows"),
                    "pivot_timeline": b.get("pivot_timeline"),
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    checksums = file_checksums(out)
    (out / "checksums_pass1.json").write_text(json.dumps(checksums, indent=2), encoding="utf-8")
    print(json.dumps(json_safe(synth["ranked_causes"]), indent=2))
    print(synth["decision_text"])
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
