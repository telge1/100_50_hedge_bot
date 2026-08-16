"""Map frozen wave-fade event rows → ClickHouse ``Signal`` records."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import pandas as pd

from signal_generator.db.signals import Signal
from signal_generator.pipeline.signal_id import deterministic_signal_id
from signal_generator.pipeline.trade_plan import (
    PRICE_SOURCE,
    levels_for_entry,
    merge_trade_plan_into_metadata,
    trade_plan_dict_from_row,
)
from signal_generator.pipeline.versions import (
    EDGES_VERSION,
    GENERATOR_VERSION,
    MODE_SHADOW,
    SIGNAL_TYPE,
    STRATEGY_VERSION,
)


def _utc(ts: Any) -> datetime:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def wave_event_to_signal(
    row: Mapping[str, Any] | pd.Series,
    *,
    symbol: str,
    timeframe: str,
    mode: str = MODE_SHADOW,
    generator_version: str = GENERATOR_VERSION,
    strategy_version: str = STRATEGY_VERSION,
    edges_version: str = EDGES_VERSION,
    selected: bool = False,
    selection_reason: str = "CANDIDATE_RAW",
    trend_by_tf: Mapping[str, str | None] | None = None,
) -> Signal:
    """Convert one annotated wave-fade row into a persistent Signal.

    ``signal_price`` is the frozen T0 entry (``resolve_entries`` → 1m open
    strictly after confirmation) when ``entry_valid``. Wave-end HTF close
    (``end_price``) is diagnostic only and must not silently replace entry.
    """
    side = str(row["side"]).upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"invalid side {side!r}")

    conf = _utc(row["confirmation_available_at"])
    # End bar open ≈ confirmation - TF duration; prefer start of end bar from end_ts
    if "end_ts" in row and pd.notna(row["end_ts"]):
        candle_open = _utc(row["end_ts"])
    else:
        candle_open = conf  # fallback; should not happen for segmented waves
    candle_close = conf

    # Prefer frozen resolve_entries fields when present
    entry_valid = bool(row.get("entry_valid", False))
    entry_price = row.get("entry_price")
    try:
        entry_f = float(entry_price) if entry_price is not None and pd.notna(entry_price) else None
    except (TypeError, ValueError):
        entry_f = None
    if entry_f is None or entry_f <= 0:
        entry_valid = False
        entry_f = None

    # If entry resolved but TP/SL columns missing, compute from frozen trade_levels
    row_dict: dict[str, Any]
    if isinstance(row, pd.Series):
        row_dict = row.to_dict()
    else:
        row_dict = dict(row)
    row_dict["side"] = side
    row_dict["signal_tf"] = timeframe
    row_dict["timeframe"] = timeframe
    row_dict["entry_valid"] = entry_valid
    row_dict["entry_price"] = entry_f
    if entry_valid and entry_f is not None:
        if row_dict.get("tp_price") is None or (
            isinstance(row_dict.get("tp_price"), float) and pd.isna(row_dict.get("tp_price"))
        ):
            lv = levels_for_entry(side=side, timeframe=timeframe, entry_price=entry_f)
            row_dict.update(lv)
            row_dict["price_source"] = PRICE_SOURCE

    plan = trade_plan_dict_from_row(row_dict)
    if entry_valid and entry_f is not None:
        signal_price = Decimal(str(entry_f))
    else:
        # Do not fall back to end_price (HTF close ≠ T0 entry). Keep 0 until finalized.
        signal_price = Decimal("0")

    is_tier_a = bool(row.get("is_tier_a", False))
    trend_bucket = str(row.get("trend_bucket", "") or "")
    eff_q = str(row.get("eff_quantile", "") or "")
    direction_wave = str(row.get("direction", "") or "")
    tier_ctx = (
        f"trend={trend_bucket};eff_q={eff_q};wave_dir={direction_wave};"
        f"edges={edges_version};global_frozen_tier_a=1"
    )

    stoch_path = row.get("stoch_path")
    wave_state = str(stoch_path) if stoch_path is not None and str(stoch_path) != "nan" else None

    trends = dict(trend_by_tf or {})
    # Same-TF trend from this event
    trends.setdefault(timeframe, trend_bucket or None)

    meta = {
        "mode": mode,
        "edges_version": edges_version,
        "available_at": conf.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "confirmation_available_at": conf.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "wave_direction": direction_wave,
        "trend_bucket": trend_bucket,
        "eff_quantile": eff_q,
        "is_q4": bool(row.get("is_q4", False)),
        "n_bars": int(row["n_bars"]) if row.get("n_bars") is not None and pd.notna(row.get("n_bars")) else None,
        "global_frozen_tier_a": True,
        "per_symbol_refit": False,
    }
    # Optional diagnostic wave-end close (not entry)
    end_price = row.get("end_price")
    if end_price is not None and not (isinstance(end_price, float) and pd.isna(end_price)):
        try:
            meta["wave_end_price"] = float(end_price)
        except (TypeError, ValueError):
            pass

    sid = deterministic_signal_id(
        strategy_version=strategy_version,
        symbol=symbol,
        timeframe=timeframe,
        candle_open_time=candle_open,
        direction=side,
        signal_type=SIGNAL_TYPE,
    )

    sig = Signal(
        symbol=symbol,
        timeframe=timeframe,
        direction=side,
        signal_type=SIGNAL_TYPE,
        signal_price=signal_price,
        candle_open_time=candle_open,
        candle_close_time=candle_close,
        generator_version=generator_version,
        strategy_version=strategy_version,
        signal_id=sid,
        generated_at=conf,  # causal: known at confirmation / available_at
        stoch_k=float(row["stoch_k_end"]) if row.get("stoch_k_end") is not None and pd.notna(row.get("stoch_k_end")) else None,
        stoch_d=None,
        wave_state=wave_state,
        tier_a=is_tier_a,
        tier_a_context=tier_ctx,
        rank_score=None,
        selected=selected,
        selection_reason=selection_reason,
        trend_15m=trends.get("15m"),
        trend_30m=trends.get("30m"),
        trend_1h=trends.get("1h"),
        trend_4h=trends.get("4h"),
        signal_bias=None,
        traded=False,
        trade_id=None,
        metadata=json.dumps(meta, separators=(",", ":")),
    )
    sig.metadata = merge_trade_plan_into_metadata(sig.metadata, plan)
    return sig
