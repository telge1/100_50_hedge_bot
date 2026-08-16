"""Build research dashboard rows from Tier-A signals + 1m timing (read-only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from signal_generator.research.one_m_entry_timing.constants import (
    DEFAULT_RESEARCH_DISPLAY_VARIANT,
    TIMING_VARIANTS,
)
from signal_generator.research.one_m_entry_timing.timing import evaluate_1m_entry_timing
from signal_generator.strategy.wave_fade.parameters import TF_BAR_MIN


def _utc(ts: Any) -> datetime:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def _iso(ts: Any | None) -> str | None:
    if ts is None:
        return None
    return _utc(ts).isoformat().replace("+00:00", "Z")


CandleLoader = Callable[[str, datetime, datetime], pd.DataFrame]


def build_research_timing_feed(
    tier_a_signals: list[dict[str, Any]],
    *,
    load_candles: CandleLoader,
    timing_variant: str = DEFAULT_RESEARCH_DISPLAY_VARIANT,
    as_of: datetime | None = None,
    warmup_minutes: int = 120,
) -> list[dict[str, Any]]:
    """Map Tier-A rows → research timing display rows.

    Read-only: never mutates ``tier_a_signals`` or writes to production tables.
    """
    variant = str(timing_variant or DEFAULT_RESEARCH_DISPLAY_VARIANT).strip()
    if variant not in TIMING_VARIANTS:
        raise ValueError(f"unknown timing_variant: {variant}")
    now = as_of or datetime.now(timezone.utc)
    now = _utc(now)

    # Group by symbol for candle loads
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for raw in tier_a_signals:
        sym = str(raw.get("symbol") or "").upper()
        if not sym:
            continue
        by_sym.setdefault(sym, []).append(raw)

    candle_cache: dict[str, pd.DataFrame] = {}
    for sym, rows in by_sym.items():
        starts = []
        ends = []
        for r in rows:
            sig_ts = r.get("candle_close_time") or r.get("generated_at") or r.get("entry_time")
            if sig_ts is None:
                continue
            t0 = _utc(r.get("entry_time") or sig_ts)
            tf = str(r.get("timeframe") or "15m")
            timeout = int(TF_BAR_MIN.get(tf, 15))
            max_hold = 24 * 60
            starts.append(t0 - timedelta(minutes=warmup_minutes))
            ends.append(t0 + timedelta(minutes=timeout + max_hold + 5))
        if not starts:
            candle_cache[sym] = pd.DataFrame()
            continue
        start = min(starts)
        end = max(ends)
        end = max(end, now + timedelta(minutes=2))
        candle_cache[sym] = load_candles(sym, start, end)

    out: list[dict[str, Any]] = []
    for raw in tier_a_signals:
        sym = str(raw.get("symbol") or "").upper()
        direction = str(raw.get("direction") or "").upper()
        tf = str(raw.get("timeframe") or "")
        sid = str(raw.get("signal_id") or "")
        signal_ts = raw.get("candle_close_time") or raw.get("generated_at")
        baseline_entry_ts = raw.get("entry_time")
        baseline_entry_price = raw.get("entry_price")
        try:
            baseline_px = float(baseline_entry_price) if baseline_entry_price is not None else None
        except (TypeError, ValueError):
            baseline_px = None

        if not sym or not direction or signal_ts is None:
            continue

        candles = candle_cache.get(sym, pd.DataFrame())
        timing = evaluate_1m_entry_timing(
            direction=direction,
            signal_tf=tf or "15m",
            signal_ts=_utc(signal_ts),
            baseline_entry_ts=_utc(baseline_entry_ts) if baseline_entry_ts else None,
            baseline_entry_price=baseline_px,
            candles_1m=candles,
            timing_variant=variant,
            as_of=now,
        )
        td = timing.as_dict()

        # Research row — never reuse production signal_id as writable key;
        # display id is variant-scoped to avoid mix-ups.
        research_id = f"research1m:{variant}:{sid}"
        entry_ts = td["entry_ts"]
        entry_price = td["entry_price"]
        result = td["result"]
        # Pending: surface trigger state as result chip helper via display_result
        if td["trigger_state"] != "ENTRY_TRIGGERED":
            display_result = td["trigger_state"]
        else:
            display_result = result

        out.append(
            {
                "signal_id": research_id,
                "source_signal_id": sid,
                "symbol": sym,
                "timeframe": tf,
                "signal_tf": tf,
                "direction": direction,
                "trade_direction": direction,
                "tier_a": True,
                "selected": bool(raw.get("selected")),
                "signal_type": raw.get("signal_type"),
                "strategy_version": "research_1m_timing",
                "generator_version": "research_1m_entry_timing_v1",
                "research_mode": True,
                "production_strategy_unchanged": True,
                "timing_variant": variant,
                "one_m_trigger_state": td["trigger_state"],
                "1m_trigger_state": td["trigger_state"],
                "trigger_state": td["trigger_state"],
                "original_tier_a_signal_ts": _iso(signal_ts),
                "oversold_overbought_reached_at": td["oversold_overbought_reached_at"],
                "turn_confirmed_at": td["turn_confirmed_at"],
                "entry_time": entry_ts,
                "entry_ts": entry_ts,
                "entry_price": entry_price,
                "signal_price": entry_price,
                "entry_valid": entry_price is not None and entry_price > 0,
                "tp_price": td["tp_price"],
                "sl_price": td["sl_price"],
                "tp_pct": td["tp_pct"],
                "sl_pct": td["sl_pct"],
                "wait_minutes": td["wait_minutes"],
                "mae_pct": td["mae_pct"],
                "mfe_pct": td["mfe_pct"],
                "MAE": td["mae_pct"],
                "MFE": td["mfe_pct"],
                "result": display_result if td["trigger_state"] != "ENTRY_TRIGGERED" else result,
                "frozen_result": result if td["trigger_state"] == "ENTRY_TRIGGERED" else "OPEN",
                "display_result": display_result,
                "pnl_pct": td["pnl_pct"],
                "duration_seconds": td["duration_seconds"],
                "exit_time": td["exit_time"],
                "exit_price": td["exit_price"],
                "exit_reason": td["exit_reason"],
                "candle_close_time": _iso(signal_ts),
                "generated_at": _iso(signal_ts),
                "stoch_k": raw.get("stoch_k"),
                "stoch_d": raw.get("stoch_d"),
                "wave_state": raw.get("wave_state"),
                "trend_15m": raw.get("trend_15m"),
                "trend_30m": raw.get("trend_30m"),
                "trend_1h": raw.get("trend_1h"),
                "trend_4h": raw.get("trend_4h"),
                # Comparison-only baseline fields (not a second active signal)
                "original_baseline_entry_ts": _iso(baseline_entry_ts) if baseline_entry_ts else None,
                "original_baseline_entry_price": baseline_px,
                "baseline_comparison_only": True,
                "is_demo": False,
                "price_incomplete": entry_price is None,
                "feed_source": "RESEARCH_1M_TIMING",
            }
        )
    return out
