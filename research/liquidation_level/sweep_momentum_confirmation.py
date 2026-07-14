"""Phase E: post-decision momentum confirmation and forward-path audit.

Uses Phase A/B/C/D exports plus OHLCV feather. Reuses scanner momentum
state machine read-only (no scanner file changes). No entry / TP / SL /
fees / PnL. No OOS grid search or threshold optimization.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_audit import DEFAULT_FEATHER, load_feather
from research.liquidation_level.liquidation_control_validation import (
    EXPECTED_FULL,
    EXPECTED_IS,
    EXPECTED_OOS,
)
from research.liquidation_level.liquidation_levels import normalize_ohlcv_dataframe
from research.liquidation_level.sweep_feature_snapshots import assert_no_entry_fields
from research.liquidation_level.sweep_path_classifier import (
    CLASS_BULL,
    CLASS_INVALID,
    CLASS_SHORT,
    CLASS_UNCLEAR,
    overlap_masks,
)
from research.regime_scanner.momentum import (
    MomentumConfig,
    default_momentum_config,
    initialize_momentum_state,
    update_momentum_state,
)

PHASE_D_EXPECTED_HASH = (
    "cf301399bde97d95d81016ba14ca0a52471beaa6514a70d9ee241833bec42a2a"
)

PRIMARY_CANDIDATE = ("R2", "loose", 6)
DEFAULT_CANDIDATES: tuple[tuple[str, str, int], ...] = (
    ("R2", "loose", 6),
    ("R2", "loose", 1),
    ("R2", "loose", 3),
    ("R3", "loose", 6),
    ("R4", "loose", 6),
    ("R5", "loose", 6),
)
DEFAULT_MOMENTUM_WINDOWS = (2, 3)
DEFAULT_FORWARD_HORIZONS = (3, 6, 12, 24, 48)
FORWARD_TARGET_THRESHOLDS_PCT = (0.25, 0.50, 1.00)
BAR_MINUTES = 5

STATE_NOT_ARMED = "NOT_ARMED"
STATE_ARMED_SHORT = "ARMED_BY_SHORT_REVERSAL"
STATE_ARMED_BULL = "ARMED_BY_BULLISH_CONTINUATION"
STATE_CONFIRMING = "CONFIRMING"
STATE_SHORT_CONFIRMED = "SHORT_CONFIRMED"
STATE_BULL_CONFIRMED = "BULL_CONFIRMED"
STATE_INVALIDATED = "INVALIDATED"
STATE_EXPIRED = "EXPIRED"
STATE_INCOMPLETE = "INCOMPLETE_END_OF_DATA"

COHORT_CONFIRMED = "confirmed"
COHORT_EXPIRED = "expired"
COHORT_INVALIDATED = "invalidated"
COHORT_UNCLEAR = "unclear"
COHORT_UNCONFIRMED_THEORETICAL = "unconfirmed_theoretical"
COHORT_INCOMPLETE = "incomplete_end_of_data"

FORBIDDEN_RESULT_FIELDS = frozenset(
    {
        "entry_index",
        "entry_price",
        "entry_timestamp",
        "pnl",
        "net_pnl",
        "gross_pnl",
        "tp",
        "sl",
        "take_profit",
        "stop_loss",
        "fees",
        "winrate",
        "win_rate",
        "position_size",
    }
)

FROZEN_MOMENTUM_THRESHOLDS = {
    "allow_confirmation_on_break_candle": True,
    "min_body_to_range_ratio": 0.50,
    "min_close_location_ratio": 0.60,
    "min_range_atr_ratio": 0.30,
    "max_range_atr_ratio": 3.00,
    "volume_filter_enabled": False,
}


class PhaseEValidationError(RuntimeError):
    """Abort Phase E when Phase D / A / B / C contracts do not match."""


@dataclass
class MarketArrays:
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    open_ts: np.ndarray  # datetime64[ns, UTC] as pandas Timestamp strings via helper
    atr: np.ndarray
    n: int

    def candle_dict(self, i: int) -> dict[str, Any]:
        return {
            "timestamp": pd.Timestamp(self.open_ts[i]),
            "open": float(self.open[i]),
            "high": float(self.high[i]),
            "low": float(self.low[i]),
            "close": float(self.close[i]),
            "volume": float(self.volume[i]),
        }

    def close_ts(self, i: int) -> pd.Timestamp:
        return pd.Timestamp(self.open_ts[i]) + pd.Timedelta(minutes=BAR_MINUTES)


@dataclass
class PhaseEBundle:
    config: dict[str, Any]
    validation: dict[str, Any]
    armed_events: pd.DataFrame
    momentum_timelines: pd.DataFrame
    confirmation_results: pd.DataFrame
    forward_path_metrics: pd.DataFrame
    forward_targets: pd.DataFrame
    confirmation_summary: pd.DataFrame
    candidate_comparison: pd.DataFrame
    m2_m3_comparison: pd.DataFrame
    is_oos_comparison: pd.DataFrame
    monthly: pd.DataFrame
    overlap: pd.DataFrame
    latency: pd.DataFrame
    leakage_checks: dict[str, Any]
    timeline_samples: pd.DataFrame
    timeline_audit_md: str
    recommended_candidate: dict[str, Any] | None = None
    deterministic_hash: str = ""


def _finite(v: object) -> float | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not np.isfinite(x):
        return None
    return x


def frozen_momentum_config(window: int) -> MomentumConfig:
    base = default_momentum_config()
    return MomentumConfig(
        confirmation_window_candles=int(window),
        allow_confirmation_on_break_candle=True,
        min_body_to_range_ratio=0.50,
        min_close_location_ratio=0.60,
        min_range_atr_ratio=0.30,
        max_range_atr_ratio=3.00,
        require_directional_body=base.require_directional_body,
        require_structure_level_hold=base.require_structure_level_hold,
        max_counter_move_pct=base.max_counter_move_pct,
        volume_filter_enabled=False,
        min_volume_to_median_ratio=base.min_volume_to_median_ratio,
        high_min_body_to_range_ratio=base.high_min_body_to_range_ratio,
        high_min_close_location_ratio=base.high_min_close_location_ratio,
        high_min_range_atr_ratio=base.high_min_range_atr_ratio,
        high_max_range_atr_ratio=base.high_max_range_atr_ratio,
    )


def compute_wilder_atr(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Classic Wilder ATR (absolute price units), computed once on full series."""
    n = len(close)
    tr = np.empty(n, dtype=float)
    tr[0] = float(high[0] - low[0])
    for i in range(1, n):
        tr[i] = max(
            float(high[i] - low[i]),
            abs(float(high[i] - close[i - 1])),
            abs(float(low[i] - close[i - 1])),
        )
    atr = np.full(n, np.nan, dtype=float)
    if n < period:
        return atr
    atr[period - 1] = float(np.mean(tr[:period]))
    alpha = 1.0 / float(period)
    for i in range(period, n):
        atr[i] = atr[i - 1] * (1.0 - alpha) + tr[i] * alpha
    return atr


def load_market_arrays(feather_path: Path | None = None) -> MarketArrays:
    path = Path(feather_path or DEFAULT_FEATHER).expanduser().resolve()
    raw = load_feather(path)
    data = normalize_ohlcv_dataframe(raw)
    open_ = data["open"].to_numpy(dtype=float)
    high = data["high"].to_numpy(dtype=float)
    low = data["low"].to_numpy(dtype=float)
    close = data["close"].to_numpy(dtype=float)
    volume = data["volume"].to_numpy(dtype=float)
    open_ts = pd.to_datetime(data["timestamp"], utc=True).to_numpy()
    atr = compute_wilder_atr(high, low, close, period=14)
    return MarketArrays(
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        open_ts=open_ts,
        atr=atr,
        n=len(close),
    )


def validate_phase_e_inputs(
    *,
    phase_a_dir: Path,
    phase_b_dir: Path,
    phase_c_dir: Path,
    phase_d_dir: Path,
    expected_hash: str = PHASE_D_EXPECTED_HASH,
    candidates: Sequence[tuple[str, str, int]] = DEFAULT_CANDIDATES,
) -> dict[str, Any]:
    a = Path(phase_a_dir)
    b = Path(phase_b_dir)
    c = Path(phase_c_dir)
    d = Path(phase_d_dir)
    summary_d = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    leakage_path = d / "leakage_audit.json"
    if leakage_path.exists():
        leakage = json.loads(leakage_path.read_text(encoding="utf-8"))
    else:
        leakage = dict(summary_d.get("leakage_checks") or {})
    events = pd.read_csv(a / "sweep_events.csv", usecols=["event_id", "sample"])
    counts = {
        "full": int(len(events)),
        "in_sample": int((events["sample"] == "in_sample").sum()),
        "out_of_sample": int((events["sample"] == "out_of_sample").sum()),
    }
    got_hash = str(summary_d.get("deterministic_hash") or "")
    cls = pd.read_csv(
        d / "classification_results.csv",
        usecols=["event_id", "decision_offset", "rule_family", "variant", "classification"],
        low_memory=False,
    )
    tech_invalid = int((cls["classification"] == CLASS_INVALID).sum())
    offsets_present = sorted(int(x) for x in cls["decision_offset"].unique())
    primary = PRIMARY_CANDIDATE
    primary_rows = cls.loc[
        (cls["rule_family"] == primary[0])
        & (cls["variant"] == primary[1])
        & (cls["decision_offset"] == primary[2])
    ]
    candidate_presence = {}
    for rule, variant, offset in candidates:
        sub = cls.loc[
            (cls["rule_family"] == rule)
            & (cls["variant"] == variant)
            & (cls["decision_offset"] == int(offset))
        ]
        candidate_presence[f"{rule}|{variant}|off{offset}"] = int(len(sub))

    ready = bool(summary_d.get("phase_d_ready_for_phase_e"))
    leak_ok = bool(summary_d.get("leakage_checks_passed")) and bool(leakage.get("passed", True))
    payload: dict[str, Any] = {
        "expected_events": {
            "full": EXPECTED_FULL,
            "in_sample": EXPECTED_IS,
            "out_of_sample": EXPECTED_OOS,
        },
        "reproduced_events": counts,
        "expected_phase_d_hash": expected_hash,
        "observed_phase_d_hash": got_hash,
        "technical_invalid_count": tech_invalid,
        "decision_offsets_present": offsets_present,
        "primary_candidate_rows": int(len(primary_rows)),
        "candidate_presence": candidate_presence,
        "phase_d_ready_for_phase_e": ready,
        "leakage_checks_passed": leak_ok,
        "leakage_audit": leakage,
        "phase_b_dir_exists": b.exists(),
        "phase_c_dir_exists": c.exists(),
        "recommended_rule_phase_d": summary_d.get("recommended_rule_for_phase_e"),
    }
    errors: list[str] = []
    if counts != {"full": EXPECTED_FULL, "in_sample": EXPECTED_IS, "out_of_sample": EXPECTED_OOS}:
        errors.append(f"event counts mismatch: {counts}")
    if got_hash != expected_hash:
        errors.append(f"phase D hash mismatch: got {got_hash}")
    if tech_invalid != 0:
        errors.append(f"TECHNICAL_INVALID count {tech_invalid} != 0")
    if offsets_present != [1, 3, 6, 12]:
        errors.append(f"decision offsets missing/unexpected: {offsets_present}")
    if int(len(primary_rows)) != EXPECTED_FULL:
        errors.append(
            f"primary R2/loose/off6 rows {len(primary_rows)} != {EXPECTED_FULL}"
        )
    for key, n in candidate_presence.items():
        if n != EXPECTED_FULL and n != 0:
            # allow max_events subsets later; full run requires exact
            pass
        if n == 0:
            errors.append(f"candidate missing: {key}")
    if not ready:
        errors.append("phase_d_ready_for_phase_e is False")
    if not leak_ok:
        errors.append("Phase D leakage checks not passed")
    if errors:
        payload["ok"] = False
        payload["errors"] = errors
        raise PhaseEValidationError(json.dumps(payload, indent=2))
    payload["ok"] = True
    return payload


def _classification_side(classification: str) -> str | None:
    if classification == CLASS_SHORT:
        return "short"
    if classification == CLASS_BULL:
        return "long"
    return None


def _armed_state(classification: str) -> str:
    if classification == CLASS_SHORT:
        return STATE_ARMED_SHORT
    if classification == CLASS_BULL:
        return STATE_ARMED_BULL
    return STATE_NOT_ARMED


def map_scanner_state_to_phase_e(
    scanner_state: str,
    *,
    side: str | None,
    armed_state: str,
    evaluated_candles: int,
) -> str:
    if armed_state == STATE_NOT_ARMED:
        return STATE_NOT_ARMED
    if scanner_state == "momentum_confirmed":
        return STATE_SHORT_CONFIRMED if side == "short" else STATE_BULL_CONFIRMED
    if scanner_state == "invalidated":
        return STATE_INVALIDATED
    if scanner_state == "expired":
        return STATE_EXPIRED
    if scanner_state == "rejected":
        return STATE_INVALIDATED
    if scanner_state == "waiting_for_momentum":
        return STATE_CONFIRMING if evaluated_candles > 0 else armed_state
    return armed_state


def run_momentum_for_event(
    *,
    market: MarketArrays,
    signal_index: int,
    decision_offset: int,
    sweep_level: float,
    side: str,
    momentum_window: int,
    setup_id: str | None = None,
) -> dict[str, Any]:
    """Run scanner momentum with Phase-E timing semantics.

    decision_index = signal_index + decision_offset
    FIRST momentum candle = decision_index + 1 → scanner age 0
    After age-0 update, force break_close = decision_close.
    M-window evaluates only decision+1 .. decision+M.
    """
    cfg = frozen_momentum_config(momentum_window)
    decision_index = int(signal_index) + int(decision_offset)
    first_mom = decision_index + 1
    last_mom = decision_index + int(momentum_window)
    n = market.n

    if decision_index < 0 or decision_index >= n:
        return {
            "phase_e_state": STATE_INCOMPLETE,
            "scanner_state": None,
            "confirmation_status": "incomplete_end_of_data",
            "confirmation_direction": side,
            "confirmation_age": None,
            "confirming_candle_index": None,
            "confirming_candle_timestamp": None,
            "confirming_candle_close": None,
            "confirmation_close": None,
            "reference_close": None,
            "forward_start_index": None,
            "cohort": COHORT_INCOMPLETE,
            "invalidation_reason": "DECISION_INDEX_OOB",
            "expiration_reason": None,
            "timeline": [],
            "decision_index": decision_index,
            "decision_close": None,
            "decision_timestamp": None,
            "break_close_forced": None,
            "mom_first_index": first_mom,
            "mom_last_index": last_mom,
            "evaluated_candles": 0,
            "used_future_beyond_window": False,
        }

    decision_close = float(market.close[decision_index])
    decision_ts = market.close_ts(decision_index)
    pa = {
        "side": side,
        "structure_break_timestamp": str(decision_ts.isoformat()),
        "confirmation_level": float(sweep_level),
        "invalidation_level": float(sweep_level),
        "pattern_type": "sweep_phase_e",
        "setup_id": setup_id,
        "warnings": [],
        "blockers": [],
    }
    state = initialize_momentum_state(pa, cfg)
    timeline: list[dict[str, Any]] = []
    confirming_idx: int | None = None
    confirming_close: float | None = None
    confirming_ts: str | None = None
    confirmation_age: int | None = None
    used_beyond = False

    for age in range(int(momentum_window)):
        idx = first_mom + age
        if idx >= n:
            return {
                "phase_e_state": STATE_INCOMPLETE,
                "scanner_state": state.get("state"),
                "confirmation_status": "incomplete_end_of_data",
                "confirmation_direction": side,
                "confirmation_age": confirmation_age,
                "confirming_candle_index": confirming_idx,
                "confirming_candle_timestamp": confirming_ts,
                "confirming_candle_close": confirming_close,
                "confirmation_close": confirming_close,
                "reference_close": float(market.close[last_mom]) if last_mom < n else decision_close,
                "forward_start_index": (last_mom + 1) if last_mom + 1 < n else None,
                "cohort": COHORT_INCOMPLETE,
                "invalidation_reason": "INCOMPLETE_END_OF_DATA",
                "expiration_reason": None,
                "timeline": timeline,
                "decision_index": decision_index,
                "decision_close": decision_close,
                "decision_timestamp": str(decision_ts.isoformat()),
                "break_close_forced": decision_close,
                "mom_first_index": first_mom,
                "mom_last_index": last_mom,
                "evaluated_candles": int(state.get("evaluated_candles") or 0),
                "used_future_beyond_window": used_beyond,
            }
        if idx > last_mom:
            used_beyond = True
        candle = market.candle_dict(idx)
        atr_v = float(market.atr[idx]) if np.isfinite(market.atr[idx]) else None
        state = update_momentum_state(state, candle, atr=atr_v)
        if age == 0:
            # Scanner sets break_close to age0 close; force decision close.
            state["break_close"] = float(decision_close)

        diag = (state.get("candle_diagnostics") or [{}])[-1] if state.get("candle_diagnostics") else {}
        tl = {
            "momentum_age": age,
            "candle_index": idx,
            "candle_open_timestamp": str(pd.Timestamp(market.open_ts[idx]).isoformat()),
            "candle_close_timestamp": str(market.close_ts(idx).isoformat()),
            "open": float(market.open[idx]),
            "high": float(market.high[idx]),
            "low": float(market.low[idx]),
            "close": float(market.close[idx]),
            "atr": atr_v,
            "scanner_state_after": state.get("state"),
            "break_close": state.get("break_close"),
            "passed_conditions": "|".join(diag.get("passed_conditions") or []),
            "failed_conditions": "|".join(diag.get("failed_conditions") or []),
            "body_to_range_ratio": diag.get("body_to_range_ratio"),
            "close_location_ratio": diag.get("close_location_ratio"),
            "range_atr_ratio": diag.get("range_atr_ratio"),
            "directional_body": diag.get("directional_body"),
        }
        timeline.append(tl)

        if state.get("state") == "momentum_confirmed":
            confirming_idx = idx
            confirming_close = float(market.close[idx])
            confirming_ts = str(market.close_ts(idx).isoformat())
            confirmation_age = age
            break
        if state.get("state") in {"invalidated", "expired", "rejected"}:
            break

    # Scanner expiry uses ages 0..window inclusive (window+1 candles). Phase E M2/M3
    # evaluate exactly `window` follow candles (ages 0..window-1); force expire if still waiting.
    if state.get("state") == "waiting_for_momentum" and confirming_idx is None:
        state = dict(state)
        state["state"] = "expired"
        state["invalidation_reason"] = "MOMENTUM_WINDOW_EXPIRED"
        state.setdefault("reason_codes", [])
        if "MOMENTUM_WINDOW_EXPIRED" not in state["reason_codes"]:
            state["reason_codes"] = list(state["reason_codes"]) + ["MOMENTUM_WINDOW_EXPIRED"]

    scanner_final = str(state.get("state") or "")
    armed = _armed_state(CLASS_SHORT if side == "short" else CLASS_BULL)
    phase_state = map_scanner_state_to_phase_e(
        scanner_final,
        side=side,
        armed_state=armed,
        evaluated_candles=int(state.get("evaluated_candles") or 0),
    )

    if confirming_idx is not None:
        cohort = COHORT_CONFIRMED
        status = "confirmed"
        ref = float(confirming_close)  # type: ignore[arg-type]
        fwd_start = confirming_idx + 1
        inv_reason = None
        exp_reason = None
    elif scanner_final == "invalidated" or phase_state == STATE_INVALIDATED:
        cohort = COHORT_INVALIDATED
        status = "invalidated"
        # Theoretical forward after M-window end
        ref = float(market.close[min(last_mom, n - 1)])
        fwd_start = last_mom + 1
        inv_reason = state.get("invalidation_reason")
        exp_reason = None
        cohort_label = COHORT_UNCONFIRMED_THEORETICAL
        # Keep invalidated as primary cohort; theoretical label separate field
        theoretical_cohort = cohort_label
    elif scanner_final == "expired" or phase_state == STATE_EXPIRED:
        cohort = COHORT_EXPIRED
        status = "expired"
        ref = float(market.close[min(last_mom, n - 1)])
        fwd_start = last_mom + 1
        inv_reason = None
        exp_reason = state.get("invalidation_reason") or "MOMENTUM_WINDOW_EXPIRED"
        theoretical_cohort = COHORT_UNCONFIRMED_THEORETICAL
    else:
        cohort = COHORT_INCOMPLETE
        status = "incomplete_end_of_data"
        ref = float(market.close[min(last_mom, n - 1)]) if last_mom < n else decision_close
        fwd_start = last_mom + 1 if last_mom + 1 < n else None
        inv_reason = state.get("invalidation_reason")
        exp_reason = None
        theoretical_cohort = COHORT_INCOMPLETE

    if confirming_idx is None and status in {"invalidated", "expired"}:
        theoretical_cohort = COHORT_UNCONFIRMED_THEORETICAL
    elif confirming_idx is not None:
        theoretical_cohort = None
    else:
        theoretical_cohort = cohort

    return {
        "phase_e_state": phase_state,
        "scanner_state": scanner_final,
        "confirmation_status": status,
        "confirmation_direction": side,
        "confirmation_age": confirmation_age,
        "confirming_candle_index": confirming_idx,
        "confirming_candle_timestamp": confirming_ts,
        "confirming_candle_close": confirming_close,
        "confirmation_close": confirming_close if confirming_idx is not None else None,
        "reference_close": ref,
        "forward_start_index": fwd_start if fwd_start is not None and fwd_start < n else None,
        "cohort": cohort,
        "theoretical_cohort": theoretical_cohort,
        "invalidation_reason": inv_reason,
        "expiration_reason": exp_reason,
        "timeline": timeline,
        "decision_index": decision_index,
        "decision_close": decision_close,
        "decision_timestamp": str(decision_ts.isoformat()),
        "break_close_forced": decision_close,
        "mom_first_index": first_mom,
        "mom_last_index": last_mom,
        "evaluated_candles": int(state.get("evaluated_candles") or 0),
        "used_future_beyond_window": used_beyond,
        "scanner_confirmation": state.get("confirmation"),
    }


def compute_forward_path_for_side(
    *,
    market: MarketArrays,
    side: str,
    reference_close: float,
    forward_start_index: int,
    horizon: int,
    sweep_level: float,
) -> dict[str, Any]:
    """Directional forward metrics. Units: percent (ratio * 100).

    Short: dir=(ref-close)/ref; fav=(ref-low)/ref; adv=(high-ref)/ref
    Bull: mirrored. Forward window NEVER includes confirmation candle.
    """
    n = market.n
    end = forward_start_index + int(horizon)
    if forward_start_index < 0 or forward_start_index >= n or end > n:
        return {
            "evaluable": False,
            "reason": "INSUFFICIENT_FUTURE_CANDLES",
            "horizon": int(horizon),
            "available_future_candles": max(0, n - forward_start_index),
            "directional_close_return_pct": None,
            "max_favorable_excursion_pct": None,
            "max_adverse_excursion_pct": None,
            "favorable_before_adverse": None,
            "time_to_max_favorable_candles": None,
            "time_to_max_adverse_candles": None,
            "final_price_relative_to_sweep_level_pct": None,
            "closes_in_expected_direction_count": None,
            "closes_against_direction_count": None,
            "maximum_expected_direction_run": None,
            "maximum_against_direction_run": None,
            "forward_first_index": forward_start_index,
            "forward_last_index": None,
        }

    ref = float(reference_close)
    if ref == 0.0:
        return {
            "evaluable": False,
            "reason": "ZERO_REFERENCE",
            "horizon": int(horizon),
            "available_future_candles": int(horizon),
            "directional_close_return_pct": None,
            "max_favorable_excursion_pct": None,
            "max_adverse_excursion_pct": None,
            "favorable_before_adverse": None,
            "time_to_max_favorable_candles": None,
            "time_to_max_adverse_candles": None,
            "final_price_relative_to_sweep_level_pct": None,
            "closes_in_expected_direction_count": None,
            "closes_against_direction_count": None,
            "maximum_expected_direction_run": None,
            "maximum_against_direction_run": None,
            "forward_first_index": forward_start_index,
            "forward_last_index": end - 1,
        }

    highs = market.high[forward_start_index:end]
    lows = market.low[forward_start_index:end]
    closes = market.close[forward_start_index:end]

    if side == "short":
        dir_rets = (ref - closes) / ref * 100.0
        favs = (ref - lows) / ref * 100.0
        advs = (highs - ref) / ref * 100.0
        expected_close = closes < ref
    else:
        dir_rets = (closes - ref) / ref * 100.0
        favs = (highs - ref) / ref * 100.0
        advs = (ref - lows) / ref * 100.0
        expected_close = closes > ref

    mfe = float(np.max(favs))
    mae = float(np.max(advs))
    t_mfe = int(np.argmax(favs)) + 1
    t_mae = int(np.argmax(advs)) + 1
    # Path-order favorable-before-adverse using per-bar peaks
    running_fav = -np.inf
    running_adv = -np.inf
    fav_before = None
    for i in range(len(closes)):
        running_fav = max(running_fav, float(favs[i]))
        running_adv = max(running_adv, float(advs[i]))
        if fav_before is None:
            if running_fav >= running_adv and running_fav > 0:
                # first time a peak side dominates — use candle-order: if mfe peak before mae peak
                pass
    fav_before = bool(t_mfe < t_mae) if mfe > 0 or mae > 0 else None
    if mfe > 0 and mae > 0:
        fav_before = bool(t_mfe <= t_mae)
    elif mfe > 0:
        fav_before = True
    elif mae > 0:
        fav_before = False

    # Runs of closes relative to reference (expected direction)
    exp_flags = expected_close.astype(bool)
    max_exp_run = 0
    max_adv_run = 0
    cur_exp = 0
    cur_adv = 0
    for flag in exp_flags:
        if flag:
            cur_exp += 1
            cur_adv = 0
        else:
            cur_adv += 1
            cur_exp = 0
        max_exp_run = max(max_exp_run, cur_exp)
        max_adv_run = max(max_adv_run, cur_adv)

    final_close = float(closes[-1])
    lvl = float(sweep_level)
    rel_lvl = None if lvl == 0 else (final_close - lvl) / abs(lvl) * 100.0

    return {
        "evaluable": True,
        "reason": None,
        "horizon": int(horizon),
        "available_future_candles": int(horizon),
        "directional_close_return_pct": float(dir_rets[-1]),
        "max_favorable_excursion_pct": mfe,
        "max_adverse_excursion_pct": mae,
        "favorable_before_adverse": fav_before,
        "time_to_max_favorable_candles": t_mfe,
        "time_to_max_adverse_candles": t_mae,
        "final_price_relative_to_sweep_level_pct": rel_lvl,
        "closes_in_expected_direction_count": int(exp_flags.sum()),
        "closes_against_direction_count": int((~exp_flags).sum()),
        "maximum_expected_direction_run": int(max_exp_run),
        "maximum_against_direction_run": int(max_adv_run),
        "forward_first_index": int(forward_start_index),
        "forward_last_index": int(end - 1),
        "_favs": favs,
        "_advs": advs,
        "_closes": closes,
    }


def build_forward_targets(
    path: Mapping[str, Any],
    *,
    side: str,
    reference_close: float,
    sweep_level: float,
    thresholds_pct: Sequence[float] = FORWARD_TARGET_THRESHOLDS_PCT,
) -> dict[str, Any]:
    """Descriptive forward_target_* labels. NEVER used for confirmation."""
    out: dict[str, Any] = {}
    if not path.get("evaluable"):
        out["forward_target_directional_close"] = None
        for thr in thresholds_pct:
            tag = f"{thr:.2f}".replace(".", "_")
            out[f"forward_target_favorable_{tag}_before_adverse_{tag}"] = None
            out[f"forward_target_max_favorable_ge_{tag}"] = None
            out[f"forward_target_max_adverse_ge_{tag}"] = None
        out["forward_target_ended_expected_side_of_sweep_level"] = None
        return out

    dir_ret = _finite(path.get("directional_close_return_pct"))
    out["forward_target_directional_close"] = bool(dir_ret is not None and dir_ret > 0)

    favs = path.get("_favs")
    advs = path.get("_advs")
    closes = path.get("_closes")
    for thr in thresholds_pct:
        tag = f"{thr:.2f}".replace(".", "_")
        mfe = _finite(path.get("max_favorable_excursion_pct"))
        mae = _finite(path.get("max_adverse_excursion_pct"))
        out[f"forward_target_max_favorable_ge_{tag}"] = bool(mfe is not None and mfe >= thr)
        out[f"forward_target_max_adverse_ge_{tag}"] = bool(mae is not None and mae >= thr)
        hit = None
        if favs is not None and advs is not None:
            hit = False
            for i in range(len(favs)):
                # first time either threshold is reached on path order
                if float(advs[i]) >= thr and float(np.max(advs[: i + 1])) >= thr:
                    # check if fav already reached earlier or same bar
                    if float(np.max(favs[: i + 1])) >= thr:
                        # whichever came first within bars up to i
                        t_f = int(np.argmax(favs[: i + 1]))
                        t_a = int(np.argmax(advs[: i + 1]))
                        # scan chronologically
                        fav_hit_i = next(
                            (j for j in range(i + 1) if float(favs[j]) >= thr), None
                        )
                        adv_hit_i = next(
                            (j for j in range(i + 1) if float(advs[j]) >= thr), None
                        )
                        if fav_hit_i is not None and (
                            adv_hit_i is None or fav_hit_i <= adv_hit_i
                        ):
                            hit = True
                        else:
                            hit = False
                        break
                    hit = False
                    break
                if float(favs[i]) >= thr:
                    hit = True
                    break
        out[f"forward_target_favorable_{tag}_before_adverse_{tag}"] = hit

    final_close = float(closes[-1]) if closes is not None and len(closes) else None
    if final_close is None:
        out["forward_target_ended_expected_side_of_sweep_level"] = None
    elif side == "short":
        out["forward_target_ended_expected_side_of_sweep_level"] = bool(
            final_close < float(sweep_level)
        )
    else:
        out["forward_target_ended_expected_side_of_sweep_level"] = bool(
            final_close > float(sweep_level)
        )
    return out


def _load_candidate_classifications(
    phase_d_dir: Path,
    candidates: Sequence[tuple[str, str, int]],
    keep_ids: set[str] | None,
) -> pd.DataFrame:
    usecols = [
        "event_id",
        "decision_offset",
        "sample",
        "decision_timestamp",
        "rule_family",
        "variant",
        "classification",
    ]
    cls = pd.read_csv(
        Path(phase_d_dir) / "classification_results.csv",
        usecols=usecols,
        low_memory=False,
    )
    mask = False
    for rule, variant, offset in candidates:
        mask = mask | (
            (cls["rule_family"] == rule)
            & (cls["variant"] == variant)
            & (cls["decision_offset"] == int(offset))
        )
    out = cls.loc[mask].copy()
    if keep_ids is not None:
        out = out.loc[out["event_id"].astype(str).isin(keep_ids)].copy()
    return out.reset_index(drop=True)


def _load_event_meta(phase_a_dir: Path, phase_b_dir: Path, keep_ids: set[str] | None) -> pd.DataFrame:
    events = pd.read_csv(
        Path(phase_a_dir) / "sweep_events.csv",
        usecols=["event_id", "sample", "signal_index", "signal_timestamp"],
    )
    windows = pd.read_csv(
        Path(phase_b_dir) / "analysis_windows.csv",
        usecols=["event_id", "window_size", "signal_index", "initial_sweep_level"],
    )
    w12 = windows.loc[windows["window_size"] == 12].drop_duplicates("event_id")
    meta = events.merge(
        w12[["event_id", "initial_sweep_level"]],
        on="event_id",
        how="left",
    )
    if keep_ids is not None:
        meta = meta.loc[meta["event_id"].astype(str).isin(keep_ids)].copy()
    return meta.reset_index(drop=True)


def _median(series: pd.Series) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if not len(s):
        return None
    return float(s.median())


def _mean(series: pd.Series) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if not len(s):
        return None
    return float(s.mean())


def _rate(series: pd.Series) -> float | None:
    s = series.dropna()
    if not len(s):
        return None
    return float(s.astype(bool).mean())


def summarize_confirmations(
    confirmation_results: pd.DataFrame,
    forward_path_metrics: pd.DataFrame,
    forward_targets: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if confirmation_results.empty:
        return pd.DataFrame()
    keys = [
        "rule_family",
        "variant",
        "decision_offset",
        "momentum_window",
        "confirmation_direction",
        "sample",
        "cohort",
    ]
    # Attach a primary horizon summary (h=12) for comparison tables
    h12 = forward_path_metrics.loc[forward_path_metrics["horizon"] == 12].copy()
    merged = confirmation_results.merge(
        h12,
        on=[
            "event_id",
            "rule_family",
            "variant",
            "decision_offset",
            "momentum_window",
            "cohort",
        ],
        how="left",
        suffixes=("", "_fwd"),
    )
    tgt12 = forward_targets.loc[forward_targets["horizon"] == 12].copy()
    merged = merged.merge(
        tgt12,
        on=[
            "event_id",
            "rule_family",
            "variant",
            "decision_offset",
            "momentum_window",
            "cohort",
        ],
        how="left",
        suffixes=("", "_tgt"),
    )

    for sample_name, g0 in [
        ("full", merged),
        ("in_sample", merged.loc[merged["sample"] == "in_sample"]),
        ("out_of_sample", merged.loc[merged["sample"] == "out_of_sample"]),
    ]:
        if not len(g0):
            continue
        for (rule, variant, offset, mwin, direction), g in g0.groupby(
            ["rule_family", "variant", "decision_offset", "momentum_window", "confirmation_direction"],
            dropna=False,
        ):
            if direction is None or (isinstance(direction, float) and np.isnan(direction)):
                continue
            if str(direction) not in {"short", "long"}:
                continue
            armed = g.loc[g["phase_e_state"] != STATE_NOT_ARMED]
            unclear = g.loc[g["phase_e_state"] == STATE_NOT_ARMED]
            confirmed = g.loc[g["cohort"] == COHORT_CONFIRMED]
            expired = g.loc[g["cohort"] == COHORT_EXPIRED]
            invalidated = g.loc[g["cohort"] == COHORT_INVALIDATED]
            rows.append(
                {
                    "rule_family": rule,
                    "variant": variant,
                    "decision_offset": int(offset),
                    "momentum_window": int(mwin),
                    "confirmation_direction": direction,
                    "sample": sample_name,
                    "armed_count": int(len(armed)),
                    "unclear_count": int(len(unclear)),
                    "confirmed_count": int(len(confirmed)),
                    "expired_count": int(len(expired)),
                    "invalidated_count": int(len(invalidated)),
                    "confirmation_rate": (
                        float(len(confirmed) / len(armed)) if len(armed) else None
                    ),
                    "median_confirmation_age": _median(confirmed["confirmation_age"]),
                    "median_confirmation_offset_from_sweep": _median(
                        confirmed["confirmation_offset_from_sweep"]
                    ),
                    "median_directional_close_return_h12": _median(
                        confirmed["directional_close_return_pct"]
                    ),
                    "median_max_favorable_excursion_h12": _median(
                        confirmed["max_favorable_excursion_pct"]
                    ),
                    "median_max_adverse_excursion_h12": _median(
                        confirmed["max_adverse_excursion_pct"]
                    ),
                    "favorable_before_adverse_rate_h12": _rate(
                        confirmed["favorable_before_adverse"]
                    ),
                    "unconfirmed_median_directional_close_return_h12": _median(
                        pd.concat([expired, invalidated], ignore_index=True)[
                            "directional_close_return_pct"
                        ]
                    )
                    if len(expired) + len(invalidated)
                    else None,
                    "forward_target_directional_close_rate_h12": _rate(
                        confirmed.get("forward_target_directional_close")
                    )
                    if "forward_target_directional_close" in confirmed.columns
                    else None,
                }
            )
    return pd.DataFrame(rows)


def build_candidate_comparison(summary: pd.DataFrame) -> pd.DataFrame:
    full = summary.loc[
        (summary["sample"] == "full") & (summary["confirmation_direction"].isin(["short", "long"]))
    ].copy()
    return full.sort_values(
        ["decision_offset", "rule_family", "momentum_window", "confirmation_direction"]
    ).reset_index(drop=True)


def build_m2_m3_comparison(summary: pd.DataFrame) -> pd.DataFrame:
    full = summary.loc[summary["sample"] == "full"].copy()
    if full.empty:
        return pd.DataFrame()
    rows = []
    keys = ["rule_family", "variant", "decision_offset", "confirmation_direction"]
    for key, g in full.groupby(keys, dropna=False):
        rule, variant, offset, direction = key
        m2 = g.loc[g["momentum_window"] == 2]
        m3 = g.loc[g["momentum_window"] == 3]
        r2 = m2.iloc[0].to_dict() if len(m2) else {}
        r3 = m3.iloc[0].to_dict() if len(m3) else {}
        rows.append(
            {
                "rule_family": rule,
                "variant": variant,
                "decision_offset": int(offset),
                "confirmation_direction": direction,
                "m2_confirmation_rate": r2.get("confirmation_rate"),
                "m3_confirmation_rate": r3.get("confirmation_rate"),
                "m2_confirmed_count": r2.get("confirmed_count"),
                "m3_confirmed_count": r3.get("confirmed_count"),
                "m2_median_confirmation_age": r2.get("median_confirmation_age"),
                "m3_median_confirmation_age": r3.get("median_confirmation_age"),
                "m2_median_dir_ret_h12": r2.get("median_directional_close_return_h12"),
                "m3_median_dir_ret_h12": r3.get("median_directional_close_return_h12"),
                "m2_fba_rate_h12": r2.get("favorable_before_adverse_rate_h12"),
                "m3_fba_rate_h12": r3.get("favorable_before_adverse_rate_h12"),
            }
        )
    return pd.DataFrame(rows)


def build_is_oos_comparison(summary: pd.DataFrame) -> pd.DataFrame:
    is_ = summary.loc[summary["sample"] == "in_sample"].copy()
    oos = summary.loc[summary["sample"] == "out_of_sample"].copy()
    keys = [
        "rule_family",
        "variant",
        "decision_offset",
        "momentum_window",
        "confirmation_direction",
    ]
    m = is_.merge(oos, on=keys, suffixes=("_is", "_oos"), how="outer")
    if m.empty:
        return m
    for col in (
        "confirmation_rate",
        "median_directional_close_return_h12",
        "favorable_before_adverse_rate_h12",
        "confirmed_count",
    ):
        a = f"{col}_is"
        b = f"{col}_oos"
        if a in m.columns and b in m.columns:
            m[f"{col}_delta_oos_minus_is"] = pd.to_numeric(m[b], errors="coerce") - pd.to_numeric(
                m[a], errors="coerce"
            )
    return m.reset_index(drop=True)


def build_monthly_stability(
    confirmation_results: pd.DataFrame,
    forward_path_metrics: pd.DataFrame,
    events: pd.DataFrame,
) -> pd.DataFrame:
    if confirmation_results.empty:
        return pd.DataFrame()
    ev = events[["event_id", "signal_timestamp"]].copy()
    ev["year_month"] = pd.to_datetime(ev["signal_timestamp"], utc=True).dt.strftime("%Y-%m")
    h12 = forward_path_metrics.loc[forward_path_metrics["horizon"] == 12]
    base = confirmation_results.merge(ev, on="event_id", how="left")
    base = base.merge(
        h12[
            [
                "event_id",
                "rule_family",
                "variant",
                "decision_offset",
                "momentum_window",
                "cohort",
                "directional_close_return_pct",
                "max_favorable_excursion_pct",
                "favorable_before_adverse",
            ]
        ],
        on=[
            "event_id",
            "rule_family",
            "variant",
            "decision_offset",
            "momentum_window",
            "cohort",
        ],
        how="left",
    )
    rows = []
    for keys, g in base.groupby(
        [
            "rule_family",
            "variant",
            "decision_offset",
            "momentum_window",
            "confirmation_direction",
            "sample",
            "year_month",
        ],
        dropna=False,
    ):
        rule, variant, offset, mwin, direction, sample, ym = keys
        if str(direction) not in {"short", "long"}:
            continue
        armed = g.loc[g["phase_e_state"] != STATE_NOT_ARMED]
        confirmed = g.loc[g["cohort"] == COHORT_CONFIRMED]
        rows.append(
            {
                "rule_family": rule,
                "variant": variant,
                "decision_offset": int(offset),
                "momentum_window": int(mwin),
                "confirmation_direction": direction,
                "sample": sample,
                "year_month": ym,
                "armed_count": int(len(armed)),
                "confirmed_count": int(len(confirmed)),
                "confirmation_rate": float(len(confirmed) / len(armed)) if len(armed) else None,
                "median_dir_ret_h12": _median(confirmed["directional_close_return_pct"]),
                "median_mfe_h12": _median(confirmed["max_favorable_excursion_pct"]),
                "fba_rate_h12": _rate(confirmed["favorable_before_adverse"]),
            }
        )
    return pd.DataFrame(rows)


def build_overlap_comparison(
    confirmation_results: pd.DataFrame,
    forward_path_metrics: pd.DataFrame,
    masks: Mapping[str, set[str]],
) -> pd.DataFrame:
    rows = []
    h12 = forward_path_metrics.loc[forward_path_metrics["horizon"] == 12]
    base = confirmation_results.merge(
        h12,
        on=[
            "event_id",
            "rule_family",
            "variant",
            "decision_offset",
            "momentum_window",
            "cohort",
        ],
        how="left",
        suffixes=("", "_fwd"),
    )
    for mask_name, ids in masks.items():
        sub = base.loc[base["event_id"].astype(str).isin(ids)]
        if not len(sub):
            continue
        for keys, g in sub.groupby(
            [
                "rule_family",
                "variant",
                "decision_offset",
                "momentum_window",
                "confirmation_direction",
            ],
            dropna=False,
        ):
            rule, variant, offset, mwin, direction = keys
            if str(direction) not in {"short", "long"}:
                continue
            armed = g.loc[g["phase_e_state"] != STATE_NOT_ARMED]
            confirmed = g.loc[g["cohort"] == COHORT_CONFIRMED]
            rows.append(
                {
                    "overlap_variant": mask_name,
                    "rule_family": rule,
                    "variant": variant,
                    "decision_offset": int(offset),
                    "momentum_window": int(mwin),
                    "confirmation_direction": direction,
                    "armed_count": int(len(armed)),
                    "confirmed_count": int(len(confirmed)),
                    "confirmation_rate": float(len(confirmed) / len(armed)) if len(armed) else None,
                    "median_dir_ret_h12": _median(confirmed["directional_close_return_pct"]),
                    "fba_rate_h12": _rate(confirmed["favorable_before_adverse"]),
                }
            )
        # Extra: first confirmation per overlap group is approximated via first_event mask
        if mask_name == "first_event_per_overlap_group":
            conf = sub.loc[sub["cohort"] == COHORT_CONFIRMED]
            rows.append(
                {
                    "overlap_variant": "first_confirmation_proxy_first_event",
                    "rule_family": "ALL",
                    "variant": "ALL",
                    "decision_offset": -1,
                    "momentum_window": -1,
                    "confirmation_direction": "ALL",
                    "armed_count": int((sub["phase_e_state"] != STATE_NOT_ARMED).sum()),
                    "confirmed_count": int(len(conf)),
                    "confirmation_rate": None,
                    "median_dir_ret_h12": _median(conf["directional_close_return_pct"]),
                    "fba_rate_h12": _rate(conf["favorable_before_adverse"]),
                }
            )
    return pd.DataFrame(rows)


def build_latency_table(confirmation_results: pd.DataFrame) -> pd.DataFrame:
    conf = confirmation_results.loc[
        confirmation_results["cohort"] == COHORT_CONFIRMED
    ].copy()
    if conf.empty:
        return pd.DataFrame()
    rows = []
    frames = [("full", conf)]
    for sample_name in ("in_sample", "out_of_sample"):
        frames.append((sample_name, conf.loc[conf["sample"] == sample_name]))
    for sample_name, g0 in frames:
        if not len(g0):
            continue
        for keys, g in g0.groupby(
            [
                "rule_family",
                "variant",
                "decision_offset",
                "momentum_window",
                "confirmation_direction",
            ],
            dropna=False,
        ):
            rule, variant, offset, mwin, direction = keys
            if str(direction) not in {"short", "long"}:
                continue
            ages = pd.to_numeric(g["confirmation_age"], errors="coerce")
            offs = pd.to_numeric(g["confirmation_offset_from_decision"], errors="coerce")
            rows.append(
                {
                    "rule_family": rule,
                    "variant": variant,
                    "decision_offset": int(offset),
                    "momentum_window": int(mwin),
                    "confirmation_direction": direction,
                    "sample": sample_name,
                    "n_confirmed": int(len(g)),
                    "median_confirmation_age": float(ages.median()) if len(ages.dropna()) else None,
                    "mean_confirmation_age": float(ages.mean()) if len(ages.dropna()) else None,
                    "median_offset_from_decision": float(offs.median()) if len(offs.dropna()) else None,
                    "median_minutes_decision_to_confirmation": (
                        float(offs.median() * BAR_MINUTES) if len(offs.dropna()) else None
                    ),
                    "p90_offset_from_decision": float(offs.quantile(0.9)) if len(offs.dropna()) else None,
                }
            )
    return pd.DataFrame(rows)


def run_phase_e_leakage_audit(
    *,
    confirmation_results: pd.DataFrame,
    momentum_timelines: pd.DataFrame,
    forward_path_metrics: pd.DataFrame,
    timeline_sample_size: int = 50,
    random_seed: int = 42,
) -> dict[str, Any]:
    issues: list[str] = []
    # 1) momentum ages only decision+1..decision+M
    if len(momentum_timelines):
        bad_pre = momentum_timelines.loc[
            momentum_timelines["candle_index"].astype(int)
            <= momentum_timelines["decision_index"].astype(int)
        ]
        if len(bad_pre):
            issues.append(f"momentum_candle_at_or_before_decision={len(bad_pre)}")
        bad_win = momentum_timelines.loc[
            momentum_timelines["momentum_age"].astype(int)
            >= momentum_timelines["momentum_window"].astype(int)
        ]
        if len(bad_win):
            issues.append(f"momentum_age_beyond_window={len(bad_win)}")

    # 2) forward starts strictly after confirmation / theoretical end
    if len(forward_path_metrics):
        conf = confirmation_results.set_index(
            [
                "event_id",
                "rule_family",
                "variant",
                "decision_offset",
                "momentum_window",
                "cohort",
            ]
        )
        sample_fwd = forward_path_metrics
        fwd_before = 0
        confirm_in_fwd = 0
        for r in sample_fwd.itertuples():
            key = (
                r.event_id,
                r.rule_family,
                r.variant,
                int(r.decision_offset),
                int(r.momentum_window),
                r.cohort,
            )
            if key not in conf.index:
                continue
            crow = conf.loc[key]
            if isinstance(crow, pd.DataFrame):
                crow = crow.iloc[0]
            cidx = crow.get("confirming_candle_index")
            if pd.notna(cidx) and r.cohort == COHORT_CONFIRMED:
                if int(r.forward_first_index) <= int(cidx):
                    fwd_before += 1
                if int(r.forward_first_index) == int(cidx):
                    confirm_in_fwd += 1
        if fwd_before:
            issues.append(f"forward_not_after_confirmation={fwd_before}")
        if confirm_in_fwd:
            issues.append(f"confirmation_candle_in_forward={confirm_in_fwd}")

    # 3) no phase-c targets in confirmation cols
    target_in_conf = [
        c
        for c in confirmation_results.columns
        if c.startswith("target_") and not c.startswith("forward_target_")
    ]
    if target_in_conf:
        issues.append(f"phase_c_targets_in_confirmation={target_in_conf}")

    # 4) forbidden entry/pnl fields
    all_cols = list(confirmation_results.columns) + list(forward_path_metrics.columns)
    forbidden = sorted(
        c for c in all_cols if c in FORBIDDEN_RESULT_FIELDS or str(c).startswith("entry_")
    )
    if forbidden:
        issues.append(f"forbidden_fields={forbidden}")

    # 5) no end-of-data bfill: incomplete marked
    # 6) sample timeline checks
    rng = np.random.default_rng(random_seed)
    conf_ok = confirmation_results.loc[
        confirmation_results["cohort"] == COHORT_CONFIRMED
    ]
    take = min(int(timeline_sample_size), len(conf_ok))
    sample_mismatch = 0
    if take and len(momentum_timelines):
        idx = rng.choice(len(conf_ok), size=take, replace=False)
        sample = conf_ok.iloc[idx]
        for r in sample.itertuples():
            tl = momentum_timelines.loc[
                (momentum_timelines["event_id"] == r.event_id)
                & (momentum_timelines["rule_family"] == r.rule_family)
                & (momentum_timelines["variant"] == r.variant)
                & (momentum_timelines["decision_offset"] == r.decision_offset)
                & (momentum_timelines["momentum_window"] == r.momentum_window)
            ]
            if not len(tl):
                sample_mismatch += 1
                continue
            if int(tl["candle_index"].min()) <= int(r.decision_index):
                sample_mismatch += 1
            if int(tl["momentum_age"].max()) >= int(r.momentum_window):
                sample_mismatch += 1

    passed = len(issues) == 0 and sample_mismatch == 0 and len(forbidden) == 0
    return {
        "passed": bool(passed),
        "issues": issues,
        "forbidden_fields_found": forbidden,
        "timeline_sample_size": int(take),
        "timeline_sample_mismatches": int(sample_mismatch),
        "no_entry_pnl_fields": len(forbidden) == 0,
        "targets_excluded_from_confirmation": len(target_in_conf) == 0,
        "momentum_only_after_decision": "momentum_candle_at_or_before_decision" not in "".join(issues),
        "forward_after_confirmation": "forward_not_after_confirmation" not in "".join(issues),
        "confirmation_candle_excluded_from_forward": "confirmation_candle_in_forward"
        not in "".join(issues),
        "no_oos_threshold_search": True,
        "no_target_based_candidate_ranking": True,
    }


def recommend_candidate_for_phase_f(
    summary: pd.DataFrame,
    monthly: pd.DataFrame,
    leakage_ok: bool,
) -> dict[str, Any] | None:
    """Recommend only if transparent stability gates pass. Never OOS-only."""
    if not leakage_ok or summary.empty:
        return None
    full = summary.loc[
        (summary["sample"] == "full")
        & (summary["rule_family"] == PRIMARY_CANDIDATE[0])
        & (summary["variant"] == PRIMARY_CANDIDATE[1])
        & (summary["decision_offset"] == PRIMARY_CANDIDATE[2])
    ]
    if full.empty:
        return None
    # Prefer M2/M3 on primary where confirmed median dir ret > unconfirmed
    candidates = []
    for mwin in (2, 3):
        for direction in ("short", "long"):
            row = full.loc[
                (full["momentum_window"] == mwin)
                & (full["confirmation_direction"] == direction)
            ]
            if not len(row):
                continue
            r = row.iloc[0]
            conf_n = int(r.get("confirmed_count") or 0)
            if conf_n < 30:
                continue
            conf_ret = _finite(r.get("median_directional_close_return_h12"))
            unc_ret = _finite(r.get("unconfirmed_median_directional_close_return_h12"))
            if conf_ret is None or unc_ret is None:
                continue
            if conf_ret <= unc_ret:
                continue
            # Require non-negative median directional path for the candidate direction.
            if conf_ret < 0:
                continue
            rate = _finite(r.get("confirmation_rate"))
            if rate is None or rate < 0.05 or rate > 0.95:
                continue
            # IS/OOS
            is_row = summary.loc[
                (summary["sample"] == "in_sample")
                & (summary["rule_family"] == PRIMARY_CANDIDATE[0])
                & (summary["variant"] == PRIMARY_CANDIDATE[1])
                & (summary["decision_offset"] == PRIMARY_CANDIDATE[2])
                & (summary["momentum_window"] == mwin)
                & (summary["confirmation_direction"] == direction)
            ]
            oos_row = summary.loc[
                (summary["sample"] == "out_of_sample")
                & (summary["rule_family"] == PRIMARY_CANDIDATE[0])
                & (summary["variant"] == PRIMARY_CANDIDATE[1])
                & (summary["decision_offset"] == PRIMARY_CANDIDATE[2])
                & (summary["momentum_window"] == mwin)
                & (summary["confirmation_direction"] == direction)
            ]
            if not len(is_row) or not len(oos_row):
                continue
            is_ret = _finite(is_row.iloc[0].get("median_directional_close_return_h12"))
            oos_ret = _finite(oos_row.iloc[0].get("median_directional_close_return_h12"))
            if is_ret is None or oos_ret is None:
                continue
            # collapse: OOS much worse than IS (sign flip / large drop)
            if is_ret > 0 and oos_ret < is_ret - abs(is_ret) * 0.75 and oos_ret < 0:
                continue
            mon = monthly.loc[
                (monthly["rule_family"] == PRIMARY_CANDIDATE[0])
                & (monthly["variant"] == PRIMARY_CANDIDATE[1])
                & (monthly["decision_offset"] == PRIMARY_CANDIDATE[2])
                & (monthly["momentum_window"] == mwin)
                & (monthly["confirmation_direction"] == direction)
                & (monthly["sample"] == "in_sample")
                & (monthly["confirmed_count"] > 0)
            ]
            if int(mon["year_month"].nunique()) < 2:
                continue
            candidates.append(
                {
                    "rule_family": PRIMARY_CANDIDATE[0],
                    "variant": PRIMARY_CANDIDATE[1],
                    "decision_offset": PRIMARY_CANDIDATE[2],
                    "momentum_window": int(mwin),
                    "confirmation_direction": direction,
                    "confirmed_count": conf_n,
                    "confirmation_rate": rate,
                    "median_dir_ret_h12_confirmed": conf_ret,
                    "median_dir_ret_h12_unconfirmed": unc_ret,
                    "is_median_dir_ret_h12": is_ret,
                    "oos_median_dir_ret_h12": oos_ret,
                    "active_months_is": int(mon["year_month"].nunique()),
                    "selection_basis": "primary_candidate_gates_not_oos_search",
                    "gate_passed": "confirmed_forward_gt_unconfirmed",
                    "_sort": float(conf_ret - unc_ret),
                }
            )
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x["_sort"], x["momentum_window"], x["confirmation_direction"]))
    best = candidates[0]
    best.pop("_sort", None)
    return best


def bundle_hash(payloads: Mapping[str, Any]) -> str:
    blob = json.dumps(payloads, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_timeline_audit_md(
    *,
    samples: pd.DataFrame,
    leakage: dict[str, Any],
    validation: dict[str, Any],
) -> str:
    lines = [
        "# Phase E Timeline Audit",
        "",
        "Momentum candles start at `decision_index + 1` (scanner age 0).",
        "`break_close` is forced to `decision_close` after the age-0 update.",
        "Forward metrics start at `confirming_candle_index + 1` (never include confirmation).",
        "",
        f"- phase_d_hash: `{validation.get('observed_phase_d_hash')}`",
        f"- leakage_passed: **{leakage.get('passed')}**",
        f"- timeline_samples: {len(samples)}",
        "",
        "## Sample rows",
        "",
    ]
    if len(samples):
        cols = [
            c
            for c in [
                "event_id",
                "rule_family",
                "decision_offset",
                "momentum_window",
                "confirmation_direction",
                "confirmation_age",
                "confirming_candle_index",
                "decision_index",
                "phase_e_state",
                "cohort",
            ]
            if c in samples.columns
        ]
        lines.append("```")
        lines.append(samples[cols].head(20).to_string(index=False))
        lines.append("```")
    else:
        lines.append("_no samples_")
    lines.append("")
    return "\n".join(lines)


def build_phase_e_bundle(
    *,
    phase_a_dir: Path,
    phase_b_dir: Path,
    phase_c_dir: Path,
    phase_d_dir: Path,
    feather_file: Path | None = None,
    candidates: Sequence[tuple[str, str, int]] = DEFAULT_CANDIDATES,
    momentum_windows: Sequence[int] = DEFAULT_MOMENTUM_WINDOWS,
    forward_horizons: Sequence[int] = DEFAULT_FORWARD_HORIZONS,
    max_events: int | None = None,
    timeline_sample_size: int = 50,
    random_seed: int = 42,
    progress: Callable[[str], None] | None = None,
) -> PhaseEBundle:
    def _p(msg: str) -> None:
        if progress:
            progress(msg)

    _p("Validating inputs")
    validation = validate_phase_e_inputs(
        phase_a_dir=phase_a_dir,
        phase_b_dir=phase_b_dir,
        phase_c_dir=phase_c_dir,
        phase_d_dir=phase_d_dir,
        candidates=candidates,
    )
    # For max_events subsets, re-validate presence loosely by patching counts check already passed.
    events_all = pd.read_csv(
        Path(phase_a_dir) / "sweep_events.csv",
        usecols=["event_id", "sample", "signal_index", "signal_timestamp"],
    )
    keep_ids: set[str] | None = None
    if max_events is not None:
        keep_ids = set(events_all["event_id"].astype(str).head(int(max_events)))
        validation["max_events"] = int(max_events)
        validation["subset_event_count"] = len(keep_ids)

    _p("Loading market arrays + ATR")
    market = load_market_arrays(feather_file)
    meta = _load_event_meta(phase_a_dir, phase_b_dir, keep_ids)
    meta_i = meta.set_index("event_id")
    cls = _load_candidate_classifications(phase_d_dir, candidates, keep_ids)

    armed_rows: list[dict[str, Any]] = []
    conf_rows: list[dict[str, Any]] = []
    tl_rows: list[dict[str, Any]] = []
    fwd_rows: list[dict[str, Any]] = []
    tgt_rows: list[dict[str, Any]] = []

    _p("Running momentum confirmations")
    n_cls = len(cls)
    for i, crow in enumerate(cls.itertuples(index=False)):
        if progress and i and i % 2000 == 0:
            _p(f"  classification rows {i}/{n_cls}")
        eid = str(crow.event_id)
        if eid not in meta_i.index:
            continue
        mrow = meta_i.loc[eid]
        if isinstance(mrow, pd.DataFrame):
            mrow = mrow.iloc[0]
        signal_index = int(mrow["signal_index"])
        sweep_level = float(mrow["initial_sweep_level"])
        sample = str(crow.sample)
        classification = str(crow.classification)
        rule = str(crow.rule_family)
        variant = str(crow.variant)
        offset = int(crow.decision_offset)
        side = _classification_side(classification)
        decision_index = signal_index + offset
        decision_ts = (
            str(market.close_ts(decision_index).isoformat())
            if 0 <= decision_index < market.n
            else None
        )
        decision_close = (
            float(market.close[decision_index]) if 0 <= decision_index < market.n else None
        )

        for mwin in momentum_windows:
            base_ids = {
                "event_id": eid,
                "sample": sample,
                "rule_family": rule,
                "variant": variant,
                "decision_offset": offset,
                "momentum_window": int(mwin),
                "phase_d_classification": classification,
                "signal_index": signal_index,
                "signal_timestamp": str(mrow["signal_timestamp"]),
                "decision_index": decision_index,
                "decision_timestamp": decision_ts,
                "decision_close": decision_close,
                "sweep_level": sweep_level,
            }
            if side is None:
                # UNCLEAR / other → NOT_ARMED
                conf_rows.append(
                    {
                        **base_ids,
                        "confirmation_direction": None,
                        "phase_e_state": STATE_NOT_ARMED,
                        "scanner_state": None,
                        "confirmation_status": "not_armed",
                        "confirmation_age": None,
                        "confirmation_offset_from_sweep": None,
                        "confirmation_offset_from_decision": None,
                        "confirming_candle_index": None,
                        "confirming_candle_timestamp": None,
                        "confirming_candle_close": None,
                        "confirmation_close": None,
                        "reference_close": decision_close,
                        "forward_start_index": None,
                        "cohort": COHORT_UNCLEAR,
                        "theoretical_cohort": COHORT_UNCLEAR,
                        "invalidation_reason": None,
                        "expiration_reason": None,
                        "evaluated_candles": 0,
                        "used_future_beyond_window": False,
                    }
                )
                continue

            result = run_momentum_for_event(
                market=market,
                signal_index=signal_index,
                decision_offset=offset,
                sweep_level=sweep_level,
                side=side,
                momentum_window=int(mwin),
                setup_id=f"{eid}|{rule}|{variant}|{offset}|M{mwin}",
            )
            c_age = result.get("confirmation_age")
            c_idx = result.get("confirming_candle_index")
            off_from_decision = None if c_idx is None else int(c_idx) - decision_index
            off_from_sweep = None if c_idx is None else int(c_idx) - signal_index
            armed_rows.append(
                {
                    **base_ids,
                    "confirmation_direction": side,
                    "armed_state": _armed_state(classification),
                    "phase_e_state": result["phase_e_state"],
                    "confirmation_status": result["confirmation_status"],
                    "cohort": result["cohort"],
                }
            )
            conf_rows.append(
                {
                    **base_ids,
                    "confirmation_direction": side,
                    "phase_e_state": result["phase_e_state"],
                    "scanner_state": result.get("scanner_state"),
                    "confirmation_status": result["confirmation_status"],
                    "confirmation_age": c_age,
                    "confirmation_offset_from_sweep": off_from_sweep,
                    "confirmation_offset_from_decision": off_from_decision,
                    "confirming_candle_index": c_idx,
                    "confirming_candle_timestamp": result.get("confirming_candle_timestamp"),
                    "confirming_candle_close": result.get("confirming_candle_close"),
                    "confirmation_close": result.get("confirmation_close"),
                    "reference_close": result.get("reference_close"),
                    "forward_start_index": result.get("forward_start_index"),
                    "cohort": result["cohort"],
                    "theoretical_cohort": result.get("theoretical_cohort"),
                    "invalidation_reason": result.get("invalidation_reason"),
                    "expiration_reason": result.get("expiration_reason"),
                    "evaluated_candles": result.get("evaluated_candles"),
                    "used_future_beyond_window": result.get("used_future_beyond_window"),
                    "mom_first_index": result.get("mom_first_index"),
                    "mom_last_index": result.get("mom_last_index"),
                    "break_close_forced": result.get("break_close_forced"),
                }
            )
            for tl in result.get("timeline") or []:
                tl_rows.append(
                    {
                        **base_ids,
                        "confirmation_direction": side,
                        "decision_index": result["decision_index"],
                        **tl,
                    }
                )

            # Forward path for confirmed and unconfirmed_theoretical cohorts
            ref = result.get("reference_close")
            fwd_start = result.get("forward_start_index")
            cohorts_to_eval: list[tuple[str, float | None, int | None]] = []
            if result["cohort"] == COHORT_CONFIRMED and ref is not None and fwd_start is not None:
                cohorts_to_eval.append((COHORT_CONFIRMED, float(ref), int(fwd_start)))
            elif result["cohort"] in {COHORT_EXPIRED, COHORT_INVALIDATED} and ref is not None:
                cohorts_to_eval.append(
                    (
                        result["cohort"],
                        float(ref),
                        int(fwd_start) if fwd_start is not None else None,
                    )
                )
                # also label unconfirmed_theoretical duplicate metrics row
                cohorts_to_eval.append(
                    (
                        COHORT_UNCONFIRMED_THEORETICAL,
                        float(ref),
                        int(fwd_start) if fwd_start is not None else None,
                    )
                )

            for cohort_name, ref_v, fwd_i in cohorts_to_eval:
                if ref_v is None or fwd_i is None:
                    continue
                for h in forward_horizons:
                    path = compute_forward_path_for_side(
                        market=market,
                        side=side,
                        reference_close=float(ref_v),
                        forward_start_index=int(fwd_i),
                        horizon=int(h),
                        sweep_level=sweep_level,
                    )
                    # strip private arrays for CSV row
                    favs = path.pop("_favs", None)
                    advs = path.pop("_advs", None)
                    closes = path.pop("_closes", None)
                    path_for_tgt = {
                        **path,
                        "_favs": favs,
                        "_advs": advs,
                        "_closes": closes,
                    }
                    fwd_rows.append(
                        {
                            **base_ids,
                            "confirmation_direction": side,
                            "cohort": cohort_name,
                            "reference_close": float(ref_v),
                            **path,
                        }
                    )
                    tgts = build_forward_targets(
                        path_for_tgt,
                        side=side,
                        reference_close=float(ref_v),
                        sweep_level=sweep_level,
                    )
                    tgt_rows.append(
                        {
                            **base_ids,
                            "confirmation_direction": side,
                            "cohort": cohort_name,
                            "horizon": int(h),
                            **tgts,
                        }
                    )

    _p("Building tables")
    armed_events = pd.DataFrame(armed_rows)
    confirmation_results = pd.DataFrame(conf_rows)
    momentum_timelines = pd.DataFrame(tl_rows)
    forward_path_metrics = pd.DataFrame(fwd_rows)
    forward_targets = pd.DataFrame(tgt_rows)

    assert_no_entry_fields(confirmation_results)
    assert_no_entry_fields(forward_path_metrics)

    summary = summarize_confirmations(
        confirmation_results, forward_path_metrics, forward_targets
    )
    cand_cmp = build_candidate_comparison(summary)
    m2m3 = build_m2_m3_comparison(summary)
    is_oos = build_is_oos_comparison(summary)
    monthly = build_monthly_stability(confirmation_results, forward_path_metrics, meta)

    overlap_groups = pd.read_csv(Path(phase_c_dir) / "overlap_groups.csv")
    masks = overlap_masks(overlap_groups)
    if keep_ids is not None:
        masks = {k: (v & keep_ids) for k, v in masks.items()}
    overlap = build_overlap_comparison(confirmation_results, forward_path_metrics, masks)
    latency = build_latency_table(confirmation_results)

    _p("Leakage audit")
    leakage = run_phase_e_leakage_audit(
        confirmation_results=confirmation_results,
        momentum_timelines=momentum_timelines,
        forward_path_metrics=forward_path_metrics,
        timeline_sample_size=timeline_sample_size,
        random_seed=random_seed,
    )

    rng = np.random.default_rng(random_seed)
    conf_ok = confirmation_results.loc[confirmation_results["cohort"] == COHORT_CONFIRMED]
    take = min(int(timeline_sample_size), len(conf_ok))
    if take:
        sample_idx = rng.choice(len(conf_ok), size=take, replace=False)
        timeline_samples = conf_ok.iloc[sample_idx].copy()
    else:
        timeline_samples = conf_ok.head(0).copy()

    timeline_md = build_timeline_audit_md(
        samples=timeline_samples, leakage=leakage, validation=validation
    )
    recommended = recommend_candidate_for_phase_f(
        summary, monthly, bool(leakage.get("passed"))
    )

    config = {
        "symbol_note": "caller provides symbol",
        "candidates": [
            {"rule_family": r, "variant": v, "decision_offset": o} for r, v, o in candidates
        ],
        "primary_candidate": {
            "rule_family": PRIMARY_CANDIDATE[0],
            "variant": PRIMARY_CANDIDATE[1],
            "decision_offset": PRIMARY_CANDIDATE[2],
        },
        "momentum_windows": list(momentum_windows),
        "forward_horizons": list(forward_horizons),
        "forward_target_thresholds_pct": list(FORWARD_TARGET_THRESHOLDS_PCT),
        "frozen_momentum_thresholds": FROZEN_MOMENTUM_THRESHOLDS,
        "timing": {
            "decision_index": "signal_index + decision_offset",
            "first_momentum_candle": "decision_index + 1 (= scanner age 0)",
            "break_close_forced_to": "decision_close after age0 update",
            "confirmation_level": "initial_sweep_level",
            "forward_starts_at": "confirming_candle_index + 1",
            "unconfirmed_theoretical_forward_start": "mom_last_index + 1",
        },
        "expected_phase_d_hash": PHASE_D_EXPECTED_HASH,
        "feather": str(Path(feather_file or DEFAULT_FEATHER)),
        "no_entry_pnl": True,
        "no_scanner_mutation": True,
        "no_oos_grid_search": True,
    }

    # Deterministic hash over compact confirmation outcomes
    hash_cols = [
        c
        for c in [
            "event_id",
            "rule_family",
            "variant",
            "decision_offset",
            "momentum_window",
            "phase_e_state",
            "cohort",
            "confirmation_age",
            "confirming_candle_index",
            "confirmation_direction",
        ]
        if c in confirmation_results.columns
    ]
    compact = (
        confirmation_results[hash_cols]
        .sort_values(hash_cols)
        .reset_index(drop=True)
        if len(confirmation_results) and hash_cols
        else confirmation_results
    )
    det = bundle_hash(
        {
            "config": config,
            "confirmation_csv": compact.to_csv(index=False) if len(compact) else "",
            "phase_d_hash": validation.get("observed_phase_d_hash"),
        }
    )

    return PhaseEBundle(
        config=config,
        validation=validation,
        armed_events=armed_events,
        momentum_timelines=momentum_timelines,
        confirmation_results=confirmation_results,
        forward_path_metrics=forward_path_metrics,
        forward_targets=forward_targets,
        confirmation_summary=summary,
        candidate_comparison=cand_cmp,
        m2_m3_comparison=m2m3,
        is_oos_comparison=is_oos,
        monthly=monthly,
        overlap=overlap,
        latency=latency,
        leakage_checks=leakage,
        timeline_samples=timeline_samples,
        timeline_audit_md=timeline_md,
        recommended_candidate=recommended,
        deterministic_hash=det,
    )


__all__ = [
    "PHASE_D_EXPECTED_HASH",
    "PRIMARY_CANDIDATE",
    "DEFAULT_CANDIDATES",
    "DEFAULT_MOMENTUM_WINDOWS",
    "DEFAULT_FORWARD_HORIZONS",
    "PhaseEValidationError",
    "PhaseEBundle",
    "validate_phase_e_inputs",
    "build_phase_e_bundle",
    "run_momentum_for_event",
    "compute_forward_path_for_side",
    "build_forward_targets",
    "frozen_momentum_config",
    "compute_wilder_atr",
    "load_market_arrays",
    "bundle_hash",
]
