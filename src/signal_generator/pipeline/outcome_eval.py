"""Frozen BE50 trade-outcome evaluation for Tier-A signals.

Wraps ``simulate_be50_trade`` / ``trade_levels`` only — no parallel exit logic.
When frozen result is BE, runs a diagnostic No-BE50 counterfactual (original SL,
SL_FIRST) for display_result ``BE / WIN|LOSS|OPEN``.
Persisted as ``signal_outcomes`` rows with ``horizon='TRADE'``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import UUID

import numpy as np
import pandas as pd

from signal_generator.db.candles import CandleRepository
from signal_generator.db.outcomes import SignalOutcome, SignalOutcomeRepository
from signal_generator.db.signals import SignalRepository
from signal_generator.pipeline.trade_plan import parse_trade_plan_from_metadata
from signal_generator.strategy.wave_fade.adapter import bars_to_ohlcv_df
from signal_generator.strategy.wave_fade.be50 import simulate_be50_trade, trade_levels
from signal_generator.strategy.wave_fade.exits import hold_end_i, scan_exit_sl_first
from signal_generator.strategy.wave_fade.parameters import STRATEGY_MAX_HOLD_BY_TF
from signal_generator.timeframes import bars_from_mappings, ensure_utc

logger = logging.getLogger(__name__)

HORIZON_TRADE = "TRADE"
HORIZON_TRADE_NO_BE50 = "TRADE_NO_BE50"
PNL_BASIS = "gross"  # be50_gross_pct / no-BE scan gross (net = gross - FEE_PCT available in meta)

RESULT_OPEN = "OPEN"
RESULT_WIN = "WIN"
RESULT_LOSS = "LOSS"
RESULT_BE = "BE"
CLOSED_RESULTS = frozenset({RESULT_WIN, RESULT_LOSS, RESULT_BE})
# Frozen WIN/LOSS never re-evaluated; BE re-eval until CF resolves WIN/LOSS.
FROZEN_FINAL_RESULTS = frozenset({RESULT_WIN, RESULT_LOSS})
CF_FINAL_RESULTS = frozenset({RESULT_WIN, RESULT_LOSS})
NO_BE50_FINAL_RESULTS = frozenset({RESULT_WIN, RESULT_LOSS})


def _iso_z(ts: Any) -> str | None:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.isoformat().replace("+00:00", "Z")


def map_be50_reason_to_result(reason: str | None) -> str:
    """Map frozen ``be50_reason`` → dashboard result (no invented classes)."""
    r = str(reason or "")
    if r == "TP":
        return RESULT_WIN
    if r == "SL":
        return RESULT_LOSS
    if r == "BE":
        return RESULT_BE
    # TIMEOUT / DATA_MISSING → still open (need more closed 1m or entry bar)
    return RESULT_OPEN


def display_result_for(frozen_result: str, counterfactual_no_be_result: str | None) -> str:
    """Compact Result column mapping (frozen first; CF only when frozen=BE)."""
    fr = str(frozen_result or RESULT_OPEN).upper()
    if fr == RESULT_BE:
        cf = str(counterfactual_no_be_result or RESULT_OPEN).upper()
        if cf not in (RESULT_WIN, RESULT_LOSS, RESULT_OPEN):
            cf = RESULT_OPEN
        return f"BE / {cf}"
    if fr in (RESULT_WIN, RESULT_LOSS, RESULT_OPEN):
        return fr
    return RESULT_OPEN


def outcome_needs_reevaluation(prev: TradeOutcomeView | None) -> bool:
    """True if BE50 TRADE evaluator should (re)run this signal."""
    if prev is None:
        return True
    if prev.result in FROZEN_FINAL_RESULTS:
        return False
    if prev.result == RESULT_BE:
        # Frozen BE is final; keep tracking unresolved No-BE counterfactual only.
        return prev.counterfactual_no_be_result not in CF_FINAL_RESULTS
    # OPEN (or unexpected): keep updating
    return True


def outcome_needs_reevaluation_no_be50(prev: TradeOutcomeView | None) -> bool:
    """True if NO_BE50 (TRADE_NO_BE50 / active no-BE TRADE) should (re)run."""
    if prev is None:
        return True
    if prev.result in NO_BE50_FINAL_RESULTS:
        return False
    return True


@dataclass(slots=True)
class TradeOutcomeView:
    signal_id: str
    result: str  # frozen strategy result (WIN/LOSS/BE/OPEN)
    entry_time: str | None
    entry_price: float | None
    be50_activated: bool
    be50_activated_at: str | None
    be_trigger_price: float | None
    exit_time: str | None
    exit_price: float | None
    exit_reason: str | None
    pnl_pct: float | None  # frozen PnL only
    pnl_basis: str
    duration_seconds: int | None
    last_evaluated_open_time: str | None
    strategy_version: str | None
    ambiguity_flag: str
    evaluated_at: str | None
    # Diagnostic No-BE50 counterfactual (only meaningful when result=BE)
    counterfactual_no_be_result: str | None = None
    counterfactual_no_be_exit_time: str | None = None
    counterfactual_no_be_exit_price: float | None = None
    counterfactual_no_be_pnl_pct: float | None = None
    counterfactual_no_be_duration_seconds: int | None = None
    counterfactual_no_be_exit_reason: str | None = None
    display_result: str | None = None

    def __post_init__(self) -> None:
        if not self.display_result:
            object.__setattr__(
                self,
                "display_result",
                display_result_for(self.result, self.counterfactual_no_be_result),
            )

    def as_api(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "frozen_result": self.result,
            "display_result": self.display_result
            or display_result_for(self.result, self.counterfactual_no_be_result),
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "be50_activated": self.be50_activated,
            "be50_activated_at": self.be50_activated_at,
            "be_trigger_price": self.be_trigger_price,
            "exit_time": self.exit_time,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "pnl_pct": self.pnl_pct,
            "pnl_basis": self.pnl_basis,
            "duration_seconds": self.duration_seconds,
            "last_evaluated_open_time": self.last_evaluated_open_time,
            "strategy_version": self.strategy_version,
            "ambiguity_flag": self.ambiguity_flag,
            "evaluated_at": self.evaluated_at,
            "counterfactual_no_be_result": self.counterfactual_no_be_result,
            "counterfactual_no_be_exit_time": self.counterfactual_no_be_exit_time,
            "counterfactual_no_be_exit_price": self.counterfactual_no_be_exit_price,
            "counterfactual_no_be_pnl_pct": self.counterfactual_no_be_pnl_pct,
            "counterfactual_no_be_duration_seconds": self.counterfactual_no_be_duration_seconds,
            "counterfactual_no_be_exit_reason": self.counterfactual_no_be_exit_reason,
        }


def trade_outcome_from_metadata(meta: Any, *, signal_id: str = "") -> TradeOutcomeView | None:
    if meta is None:
        return None
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(meta, dict) or not meta.get("result"):
        return None
    cf_result = meta.get("counterfactual_no_be_result")
    frozen = str(meta.get("result"))
    return TradeOutcomeView(
        signal_id=str(signal_id),
        result=frozen,
        entry_time=meta.get("entry_time"),
        entry_price=_f(meta.get("entry_price")),
        be50_activated=bool(meta.get("be50_activated")),
        be50_activated_at=meta.get("be50_activated_at"),
        be_trigger_price=_f(meta.get("be_trigger_price")),
        exit_time=meta.get("exit_time"),
        exit_price=_f(meta.get("exit_price")),
        exit_reason=meta.get("exit_reason"),
        pnl_pct=_f(meta.get("pnl_pct")),
        pnl_basis=str(meta.get("pnl_basis") or PNL_BASIS),
        duration_seconds=int(meta["duration_seconds"])
        if meta.get("duration_seconds") is not None
        else None,
        last_evaluated_open_time=meta.get("last_evaluated_open_time"),
        strategy_version=meta.get("strategy_version"),
        ambiguity_flag=str(meta.get("ambiguity_flag") or ""),
        evaluated_at=meta.get("evaluated_at"),
        counterfactual_no_be_result=str(cf_result) if cf_result else None,
        counterfactual_no_be_exit_time=meta.get("counterfactual_no_be_exit_time"),
        counterfactual_no_be_exit_price=_f(meta.get("counterfactual_no_be_exit_price")),
        counterfactual_no_be_pnl_pct=_f(meta.get("counterfactual_no_be_pnl_pct")),
        counterfactual_no_be_duration_seconds=int(meta["counterfactual_no_be_duration_seconds"])
        if meta.get("counterfactual_no_be_duration_seconds") is not None
        else None,
        counterfactual_no_be_exit_reason=meta.get("counterfactual_no_be_exit_reason"),
        display_result=meta.get("display_result")
        or display_result_for(frozen, str(cf_result) if cf_result else None),
    )


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def simulate_no_be50_counterfactual(
    *,
    side: str,
    entry_price: float,
    entry_time: datetime,
    levels: Mapping[str, float],
    df: pd.DataFrame,
    hold_min: int,
) -> dict[str, Any]:
    """No-BE50 path: original SL for the whole trade, SL_FIRST. Diagnostic only.

    Continues past the frozen BE exit as if BE50 never existed.
    """
    side_u = str(side).upper()
    ts = pd.to_datetime(df["timestamp"], utc=True)
    et = pd.Timestamp(entry_time)
    if et.tzinfo is None:
        et = et.tz_localize("UTC")
    else:
        et = et.tz_convert("UTC")

    mask = ts >= et
    if not bool(mask.any()):
        return {
            "result": RESULT_OPEN,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
            "duration_seconds": None,
            "ambiguity_flag": "ENTRY_BAR_MISSING",
        }

    start_i = int(np.flatnonzero(mask.to_numpy())[0])
    if pd.Timestamp(ts.iloc[start_i]) != et:
        return {
            "result": RESULT_OPEN,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
            "duration_seconds": None,
            "ambiguity_flag": "ENTRY_BAR_MISSING",
        }

    # Naive UTC datetime64 for freeze hold_end_i arithmetic
    open_times = ts.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy(dtype="datetime64[ns]")
    n = len(df)
    end_i = hold_end_i(start_i, open_times, int(hold_min), n)

    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    tp_pct = float(levels["tp_pct"])
    sl_pct = float(levels["sl_pct"])

    exit_type, gross, exit_i, amb = scan_exit_sl_first(
        side_u, float(entry_price), highs, lows, start_i, end_i, tp_pct, sl_pct
    )

    last_ts = ensure_utc(pd.Timestamp(ts.iloc[-1]).to_pydatetime())
    entry_u = ensure_utc(entry_time)

    if exit_type is None:
        duration = int((last_ts - entry_u).total_seconds())
        return {
            "result": RESULT_OPEN,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
            "duration_seconds": max(0, duration),
            "ambiguity_flag": "",
        }

    exit_ts = ensure_utc(pd.Timestamp(ts.iloc[int(exit_i)]).to_pydatetime())
    if exit_type == "TP":
        result = RESULT_WIN
        exit_px = float(levels["tp"])
        reason = "TP"
    else:
        result = RESULT_LOSS
        exit_px = float(levels["sl"])
        reason = "SL"

    return {
        "result": result,
        "exit_time": _iso_z(exit_ts),
        "exit_price": exit_px,
        "exit_reason": reason,
        "pnl_pct": float(gross) if gross is not None else None,
        "duration_seconds": int((exit_ts - entry_u).total_seconds()),
        "ambiguity_flag": "AMBIGUOUS_INTRABAR" if amb else "",
    }


def evaluate_signal_be50(
    signal: Mapping[str, Any],
    c1m: pd.DataFrame,
    *,
    as_of: datetime | None = None,
) -> TradeOutcomeView:
    """Run frozen BE50 path sim for one signal against closed 1m OHLCV.

    ``c1m`` must use freeze columns: timestamp, open, high, low, close.
    Only bars with ``timestamp <= as_of`` (open time of last closed 1m) are used.
    When frozen result is BE, also runs No-BE50 counterfactual for display_result.
    """
    sid = str(signal.get("signal_id"))
    plan = parse_trade_plan_from_metadata(signal.get("metadata"))
    side = str(signal.get("direction") or "").upper()
    tf = str(signal.get("timeframe") or "")
    strategy_version = str(signal.get("strategy_version") or "")

    entry_price = _f(plan.get("entry_price"))
    if entry_price is None or entry_price <= 0:
        entry_price = _f(signal.get("signal_price"))
    entry_time_raw = plan.get("entry_time")
    if entry_time_raw:
        entry_time = ensure_utc(pd.Timestamp(entry_time_raw).to_pydatetime())
    else:
        # Fallback: T0 = confirmation + 1m (should be rare after entry backfill)
        conf = ensure_utc(signal["candle_close_time"])
        entry_time = conf + timedelta(minutes=1)

    be_trigger = _f(plan.get("be_trigger_price"))
    if be_trigger is None and entry_price is not None and entry_price > 0:
        lv0 = trade_levels(
            pd.Series(
                {
                    "entry_price": entry_price,
                    "side": side,
                    "highest_tf_reached": tf,
                }
            )
        )
        be_trigger = float(lv0["be_trigger"])

    now_eval = datetime.now(timezone.utc)
    as_of_u = ensure_utc(as_of) if as_of is not None else None

    def _view(**kwargs: Any) -> TradeOutcomeView:
        return TradeOutcomeView(signal_id=sid, strategy_version=strategy_version, **kwargs)

    if entry_price is None or entry_price <= 0:
        return _view(
            result=RESULT_OPEN,
            entry_time=_iso_z(entry_time),
            entry_price=None,
            be50_activated=False,
            be50_activated_at=None,
            be_trigger_price=be_trigger,
            exit_time=None,
            exit_price=None,
            exit_reason=None,
            pnl_pct=None,
            pnl_basis=PNL_BASIS,
            duration_seconds=None,
            last_evaluated_open_time=None,
            ambiguity_flag="ENTRY_PRICE_MISSING",
            evaluated_at=_iso_z(now_eval),
        )

    df = c1m
    if df is not None and not df.empty and as_of_u is not None:
        ts = pd.to_datetime(df["timestamp"], utc=True)
        # Candle close_time = open + 1m. Last closed at as_of means open = as_of - 1m.
        last_open = as_of_u - timedelta(minutes=1)
        df = df.loc[ts <= pd.Timestamp(last_open)].reset_index(drop=True)

    if df is None or df.empty:
        return _view(
            result=RESULT_OPEN,
            entry_time=_iso_z(entry_time),
            entry_price=entry_price,
            be50_activated=False,
            be50_activated_at=None,
            be_trigger_price=be_trigger,
            exit_time=None,
            exit_price=None,
            exit_reason=None,
            pnl_pct=None,
            pnl_basis=PNL_BASIS,
            duration_seconds=None,
            last_evaluated_open_time=None,
            ambiguity_flag="NO_CANDLES",
            evaluated_at=_iso_z(now_eval),
        )

    hold_min = int(STRATEGY_MAX_HOLD_BY_TF.get(tf, 24 * 60))
    # exit_time gates the walk as exit_time+10d in frozen sim; use entry so cap is wide.
    tr = pd.Series(
        {
            "entry_time": entry_time,
            "entry_price": float(entry_price),
            "side": side,
            "highest_tf_reached": tf,
            "exit_time": entry_time,
        }
    )
    levels = trade_levels(tr)
    sim = simulate_be50_trade(tr, df, levels)
    reason = str(sim.get("be50_reason") or "")
    result = map_be50_reason_to_result(reason)

    last_ts = pd.to_datetime(df.iloc[-1]["timestamp"], utc=True).to_pydatetime()
    last_ts = ensure_utc(last_ts)

    # If TIMEOUT but still within max hold from entry, treat as OPEN (more candles may come)
    if reason == "TIMEOUT":
        hold_end = entry_time + timedelta(minutes=hold_min)
        if last_ts < hold_end:
            result = RESULT_OPEN

    exit_time = sim.get("be50_exit_time")
    exit_price = sim.get("be50_exit_price")
    be50_on = bool(sim.get("be50_triggered"))
    arm_time = sim.get("be50_trigger_time")

    if result in CLOSED_RESULTS and exit_time is not None:
        xt = ensure_utc(pd.Timestamp(exit_time).to_pydatetime())
        duration = int((xt - entry_time).total_seconds())
        pnl = _f(sim.get("be50_gross_pct"))
        last_eval_open = ensure_utc(pd.Timestamp(exit_time).to_pydatetime())
    else:
        # OPEN: running duration to last evaluated closed bar open
        duration = int((last_ts - entry_time).total_seconds())
        if duration < 0:
            duration = 0
        pnl = None
        exit_time = None
        exit_price = None
        last_eval_open = last_ts
        # Keep BE activation visible while OPEN if already armed
        reason = None if result == RESULT_OPEN and reason in ("TIMEOUT", "DATA_MISSING") else reason

    cf_result = None
    cf_exit_time = None
    cf_exit_price = None
    cf_pnl = None
    cf_duration = None
    cf_exit_reason = None
    cf_amb = ""

    if result == RESULT_BE:
        cf = simulate_no_be50_counterfactual(
            side=side,
            entry_price=float(entry_price),
            entry_time=entry_time,
            levels=levels,
            df=df,
            hold_min=hold_min,
        )
        cf_result = str(cf.get("result") or RESULT_OPEN)
        cf_exit_time = cf.get("exit_time")
        cf_exit_price = _f(cf.get("exit_price"))
        cf_pnl = _f(cf.get("pnl_pct"))
        cf_duration = (
            int(cf["duration_seconds"]) if cf.get("duration_seconds") is not None else None
        )
        cf_exit_reason = cf.get("exit_reason")
        cf_amb = str(cf.get("ambiguity_flag") or "")

    amb = str(sim.get("ambiguity_flag") or "")
    if cf_amb and "AMBIGUOUS" in cf_amb and "AMBIGUOUS" not in amb:
        amb = cf_amb

    return _view(
        result=result,
        entry_time=_iso_z(entry_time),
        entry_price=float(entry_price),
        be50_activated=be50_on,
        be50_activated_at=_iso_z(arm_time) if arm_time is not None else None,
        be_trigger_price=float(levels["be_trigger"]),
        exit_time=_iso_z(exit_time) if exit_time is not None else None,
        exit_price=float(exit_price) if exit_price is not None else None,
        exit_reason=str(sim.get("be50_reason")) if result in CLOSED_RESULTS else reason,
        pnl_pct=pnl if result in CLOSED_RESULTS else None,
        pnl_basis=PNL_BASIS,
        duration_seconds=duration,
        last_evaluated_open_time=_iso_z(last_eval_open),
        ambiguity_flag=amb,
        evaluated_at=_iso_z(now_eval),
        counterfactual_no_be_result=cf_result,
        counterfactual_no_be_exit_time=cf_exit_time,
        counterfactual_no_be_exit_price=cf_exit_price,
        counterfactual_no_be_pnl_pct=cf_pnl,
        counterfactual_no_be_duration_seconds=cf_duration,
        counterfactual_no_be_exit_reason=cf_exit_reason,
    )


def _prepare_signal_path(
    signal: Mapping[str, Any],
    c1m: pd.DataFrame,
    *,
    as_of: datetime | None,
) -> tuple[dict[str, Any], pd.DataFrame | None]:
    """Shared entry / levels / as_of-truncated candle prep for exit sims."""
    sid = str(signal.get("signal_id"))
    plan = parse_trade_plan_from_metadata(signal.get("metadata"))
    side = str(signal.get("direction") or "").upper()
    tf = str(signal.get("timeframe") or "")
    strategy_version = str(signal.get("strategy_version") or "")

    entry_price = _f(plan.get("entry_price"))
    if entry_price is None or entry_price <= 0:
        entry_price = _f(signal.get("signal_price"))
    entry_time_raw = plan.get("entry_time")
    if entry_time_raw:
        entry_time = ensure_utc(pd.Timestamp(entry_time_raw).to_pydatetime())
    else:
        conf = ensure_utc(signal["candle_close_time"])
        entry_time = conf + timedelta(minutes=1)

    as_of_u = ensure_utc(as_of) if as_of is not None else None
    df = c1m
    if df is not None and not df.empty and as_of_u is not None:
        ts = pd.to_datetime(df["timestamp"], utc=True)
        last_open = as_of_u - timedelta(minutes=1)
        df = df.loc[ts <= pd.Timestamp(last_open)].reset_index(drop=True)

    hold_min = int(STRATEGY_MAX_HOLD_BY_TF.get(tf, 24 * 60))
    levels = None
    if entry_price is not None and entry_price > 0:
        tr = pd.Series(
            {
                "entry_price": float(entry_price),
                "side": side,
                "highest_tf_reached": tf,
            }
        )
        levels = trade_levels(tr)

    return {
        "signal_id": sid,
        "side": side,
        "tf": tf,
        "strategy_version": strategy_version,
        "entry_price": entry_price,
        "entry_time": entry_time,
        "hold_min": hold_min,
        "levels": levels,
        "be_trigger": float(levels["be_trigger"]) if levels else None,
    }, df


def evaluate_signal_no_be50(
    signal: Mapping[str, Any],
    c1m: pd.DataFrame,
    *,
    as_of: datetime | None = None,
) -> TradeOutcomeView:
    """Productive NO_BE50 path: original TP/SL, SL_FIRST, no BE50 state machine.

    Reuses ``simulate_no_be50_counterfactual`` (same SL_FIRST scan). Results are
    only WIN / LOSS / OPEN — never BE.
    """
    ctx, df = _prepare_signal_path(signal, c1m, as_of=as_of)
    sid = ctx["signal_id"]
    strategy_version = ctx["strategy_version"]
    entry_time = ctx["entry_time"]
    entry_price = ctx["entry_price"]
    now_eval = datetime.now(timezone.utc)

    def _view(**kwargs: Any) -> TradeOutcomeView:
        v = TradeOutcomeView(signal_id=sid, strategy_version=strategy_version, **kwargs)
        # Force display = frozen result (no BE / …)
        object.__setattr__(v, "display_result", v.result)
        return v

    if entry_price is None or entry_price <= 0 or ctx["levels"] is None:
        return _view(
            result=RESULT_OPEN,
            entry_time=_iso_z(entry_time),
            entry_price=None,
            be50_activated=False,
            be50_activated_at=None,
            be_trigger_price=ctx.get("be_trigger"),
            exit_time=None,
            exit_price=None,
            exit_reason=None,
            pnl_pct=None,
            pnl_basis=PNL_BASIS,
            duration_seconds=None,
            last_evaluated_open_time=None,
            ambiguity_flag="ENTRY_PRICE_MISSING",
            evaluated_at=_iso_z(now_eval),
        )

    if df is None or df.empty:
        return _view(
            result=RESULT_OPEN,
            entry_time=_iso_z(entry_time),
            entry_price=float(entry_price),
            be50_activated=False,
            be50_activated_at=None,
            be_trigger_price=float(ctx["levels"]["be_trigger"]),
            exit_time=None,
            exit_price=None,
            exit_reason=None,
            pnl_pct=None,
            pnl_basis=PNL_BASIS,
            duration_seconds=None,
            last_evaluated_open_time=None,
            ambiguity_flag="NO_CANDLES",
            evaluated_at=_iso_z(now_eval),
        )

    sim = simulate_no_be50_counterfactual(
        side=ctx["side"],
        entry_price=float(entry_price),
        entry_time=entry_time,
        levels=ctx["levels"],
        df=df,
        hold_min=ctx["hold_min"],
    )
    result = str(sim.get("result") or RESULT_OPEN)
    if result not in (RESULT_WIN, RESULT_LOSS, RESULT_OPEN):
        result = RESULT_OPEN

    last_ts = ensure_utc(pd.to_datetime(df.iloc[-1]["timestamp"], utc=True).to_pydatetime())
    if result in (RESULT_WIN, RESULT_LOSS):
        duration = int(sim["duration_seconds"]) if sim.get("duration_seconds") is not None else None
        pnl = _f(sim.get("pnl_pct"))
        exit_time = sim.get("exit_time")
        exit_price = _f(sim.get("exit_price"))
        exit_reason = sim.get("exit_reason")
        last_eval_open = exit_time  # already ISO-Z from simulate_no_be50
    else:
        duration = int((last_ts - entry_time).total_seconds())
        if duration < 0:
            duration = 0
        pnl = None
        exit_time = None
        exit_price = None
        exit_reason = None
        last_eval_open = _iso_z(last_ts)

    return _view(
        result=result,
        entry_time=_iso_z(entry_time),
        entry_price=float(entry_price),
        be50_activated=False,
        be50_activated_at=None,
        be_trigger_price=float(ctx["levels"]["be_trigger"]),
        exit_time=_iso_z(exit_time) if exit_time is not None else None,
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl_pct=pnl,
        pnl_basis=PNL_BASIS,
        duration_seconds=duration,
        last_evaluated_open_time=last_eval_open,
        ambiguity_flag=str(sim.get("ambiguity_flag") or ""),
        evaluated_at=_iso_z(now_eval),
    )


def outcome_to_signal_outcome(
    view: TradeOutcomeView,
    *,
    horizon: str = HORIZON_TRADE,
) -> SignalOutcome:
    is_no_be = horizon == HORIZON_TRADE_NO_BE50 or (
        view.result in (RESULT_WIN, RESULT_LOSS, RESULT_OPEN)
        and view.counterfactual_no_be_result is None
        and not view.be50_activated
        and view.result != RESULT_BE
    )
    display = view.display_result
    if horizon == HORIZON_TRADE_NO_BE50:
        display = view.result  # WIN / LOSS / OPEN only
    elif not display:
        display = display_result_for(view.result, view.counterfactual_no_be_result)

    meta = {
        "result": view.result,
        "frozen_result": view.result,
        "display_result": display,
        "exit_policy": "NO_BE50" if horizon == HORIZON_TRADE_NO_BE50 else (
            "BE50" if view.result == RESULT_BE or view.be50_activated or view.counterfactual_no_be_result
            else ("NO_BE50" if is_no_be else "BE50")
        ),
        "entry_time": view.entry_time,
        "entry_price": view.entry_price,
        "be50_activated": view.be50_activated,
        "be50_activated_at": view.be50_activated_at,
        "be_trigger_price": view.be_trigger_price,
        "exit_time": view.exit_time,
        "exit_price": view.exit_price,
        "exit_reason": view.exit_reason,
        "pnl_pct": view.pnl_pct,
        "pnl_basis": view.pnl_basis,
        "duration_seconds": view.duration_seconds,
        "last_evaluated_open_time": view.last_evaluated_open_time,
        "strategy_version": view.strategy_version,
        "ambiguity_flag": view.ambiguity_flag,
        "evaluated_at": view.evaluated_at,
        "counterfactual_no_be_result": view.counterfactual_no_be_result,
        "counterfactual_no_be_exit_time": view.counterfactual_no_be_exit_time,
        "counterfactual_no_be_exit_price": view.counterfactual_no_be_exit_price,
        "counterfactual_no_be_pnl_pct": view.counterfactual_no_be_pnl_pct,
        "counterfactual_no_be_duration_seconds": view.counterfactual_no_be_duration_seconds,
        "counterfactual_no_be_exit_reason": view.counterfactual_no_be_exit_reason,
    }
    return SignalOutcome(
        signal_id=UUID(str(view.signal_id)),
        horizon=horizon,
        evaluated_at=datetime.now(timezone.utc),
        mfe_pct=view.pnl_pct,
        mae_pct=None,
        tp_hit=view.result == RESULT_WIN,
        sl_hit=view.result == RESULT_LOSS,
        time_to_tp_seconds=view.duration_seconds if view.result == RESULT_WIN else None,
        time_to_sl_seconds=view.duration_seconds
        if view.result in (RESULT_LOSS, RESULT_BE)
        else None,
        price_after_horizon=Decimal(str(view.exit_price)) if view.exit_price is not None else None,
        metadata=json.dumps(meta, separators=(",", ":")),
    )


def summarize_trade_views(views: Sequence[TradeOutcomeView | None]) -> dict[str, Any]:
    """Aggregate performance summary (gross PnL). OPEN excluded from rates/PnL."""
    signals = 0
    wins = 0
    losses = 0
    opens = 0
    be = 0
    gross_profit = 0.0
    gross_loss = 0.0
    for v in views:
        if v is None:
            opens += 1
            signals += 1
            continue
        signals += 1
        # Prefer display_result class for BE50 mode; for NO_BE50 display==result
        disp = str(v.display_result or v.result or RESULT_OPEN).upper()
        if disp in (RESULT_WIN, "BE / WIN"):
            # For summary of active NO_BE50, display is WIN only.
            # When summarizing BE50 TRADE views, count WIN separately from BE.
            pass
        fr = str(v.result or RESULT_OPEN).upper()
        if fr == RESULT_WIN:
            wins += 1
            if v.pnl_pct is not None and float(v.pnl_pct) > 0:
                gross_profit += float(v.pnl_pct)
            elif v.pnl_pct is not None:
                gross_loss += float(v.pnl_pct)
        elif fr == RESULT_LOSS:
            losses += 1
            if v.pnl_pct is not None and float(v.pnl_pct) < 0:
                gross_loss += float(v.pnl_pct)
            elif v.pnl_pct is not None and float(v.pnl_pct) > 0:
                gross_profit += float(v.pnl_pct)
        elif fr == RESULT_BE:
            be += 1
            # Frozen BE pnl is 0 — no contribution
        else:
            opens += 1

    closed = wins + losses
    win_rate = (wins / closed * 100.0) if closed else None
    total_pnl = gross_profit + gross_loss
    return {
        "signals": signals,
        "wins": wins,
        "losses": losses,
        "open": opens,
        "be": be,
        "win_rate_pct": win_rate,
        "gross_profit_pct": gross_profit,
        "gross_loss_pct": gross_loss,
        "total_pnl_pct": total_pnl,
        "pnl_basis": PNL_BASIS,
    }


class OutcomeEvaluator:
    """Incremental evaluator: native TRADE + TRADE_NO_BE50 (never overwrites BE50 TRADE)."""

    def __init__(
        self,
        *,
        candles: CandleRepository,
        signals: SignalRepository,
        outcomes: SignalOutcomeRepository,
        exchange: str = "bybit",
    ) -> None:
        self.candles = candles
        self.signals = signals
        self.outcomes = outcomes
        self.exchange = exchange

    def evaluate_symbol(
        self,
        symbol: str,
        *,
        as_of: datetime,
        lookback_hours: int = 168,
        tier_a_only: bool = True,
    ) -> dict[str, int]:
        """Evaluate unresolved TRADE (+ BE-CF) and TRADE_NO_BE50. Idempotent."""
        from signal_generator.pipeline.versions import uses_be50_exit

        as_of = ensure_utc(as_of)
        start = as_of - timedelta(hours=max(1, lookback_hours))
        rows, _ = self.signals.query_signals(
            start=start - timedelta(days=2),
            end=as_of + timedelta(minutes=1),
            symbols=[symbol.upper()],
            tier_a=True if tier_a_only else None,
            limit=2000,
            offset=0,
        )
        candidates = []
        for r in rows:
            if tier_a_only and not bool(r.get("tier_a")):
                continue
            candidates.append(r)

        ids = [r["signal_id"] for r in candidates]
        existing_trade = self.outcomes.get_trade_outcomes_by_signal_ids(ids)
        existing_nobe = self.outcomes.get_no_be50_outcomes_by_signal_ids(ids)

        need_trade: list[dict[str, Any]] = []
        need_nobe: list[dict[str, Any]] = []
        for r in candidates:
            sid = str(r["signal_id"])
            sv = str(r.get("strategy_version") or "")
            prev_t = existing_trade.get(sid)
            prev_n = existing_nobe.get(sid)
            if uses_be50_exit(sv):
                if outcome_needs_reevaluation(prev_t):
                    need_trade.append(r)
            else:
                if outcome_needs_reevaluation_no_be50(prev_t):
                    need_trade.append(r)
            if outcome_needs_reevaluation_no_be50(prev_n):
                need_nobe.append(r)

        stats = {
            "evaluated": 0,
            "closed": 0,
            "open": 0,
            "be_cf_open": 0,
            "no_be50_evaluated": 0,
            "skipped_trade": len(candidates) - len({str(r["signal_id"]) for r in need_trade}),
            "skipped_no_be50": len(candidates) - len({str(r["signal_id"]) for r in need_nobe}),
        }

        to_load = {str(r["signal_id"]): r for r in need_trade}
        to_load.update({str(r["signal_id"]): r for r in need_nobe})
        if not to_load:
            return stats

        plan_times = []
        for r in to_load.values():
            plan = parse_trade_plan_from_metadata(r.get("metadata"))
            et = plan.get("entry_time")
            if et:
                plan_times.append(ensure_utc(pd.Timestamp(et).to_pydatetime()))
            else:
                plan_times.append(ensure_utc(r["candle_close_time"]) + timedelta(minutes=1))
        load_start = min(plan_times) - timedelta(hours=1)
        load_end = as_of + timedelta(minutes=2)
        raw = self.candles.get_candles(
            symbol.upper(), load_start, load_end, exchange=self.exchange, interval="1m"
        )
        ohlcv = bars_to_ohlcv_df(bars_from_mappings(raw))

        batch: list[SignalOutcome] = []
        trade_ids = {str(r["signal_id"]) for r in need_trade}
        nobe_ids = {str(r["signal_id"]) for r in need_nobe}

        for sid, r in to_load.items():
            sv = str(r.get("strategy_version") or "")
            if sid in trade_ids:
                if uses_be50_exit(sv):
                    view = evaluate_signal_be50(r, ohlcv, as_of=as_of)
                else:
                    view = evaluate_signal_no_be50(r, ohlcv, as_of=as_of)
                batch.append(outcome_to_signal_outcome(view, horizon=HORIZON_TRADE))
                stats["evaluated"] += 1
                if view.result in CLOSED_RESULTS or view.result in NO_BE50_FINAL_RESULTS:
                    if view.result != RESULT_OPEN:
                        stats["closed"] += 1
                if view.result == RESULT_OPEN:
                    stats["open"] += 1
                if (
                    view.result == RESULT_BE
                    and view.counterfactual_no_be_result == RESULT_OPEN
                ):
                    stats["be_cf_open"] += 1

            if sid in nobe_ids:
                view_n = evaluate_signal_no_be50(r, ohlcv, as_of=as_of)
                batch.append(outcome_to_signal_outcome(view_n, horizon=HORIZON_TRADE_NO_BE50))
                stats["no_be50_evaluated"] += 1

        if batch:
            self.outcomes.insert_signal_outcomes(batch)
        return stats
