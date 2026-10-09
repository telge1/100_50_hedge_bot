"""Long trade forward outcome — mirrored bar walk from short_trade_detail / forward_outcome."""

from __future__ import annotations

from analyze_4h_lower_short_block_v1 import HORIZON_BARS


def forward_outcome_long(bars: list[dict], entry_idx: int, entry: float, stop: float, tp: float) -> dict:
    mfe = 0.0
    mae = 0.0
    outcome = "open"
    exit_bar = None
    mfe_first_idx: int | None = None
    mae_first_idx: int | None = None
    end = min(len(bars), entry_idx + 1 + HORIZON_BARS)
    for i in range(entry_idx + 1, end):
        bar = bars[i]
        fav = (bar["high"] - entry) / entry * 100.0
        adv = (entry - bar["low"]) / entry * 100.0
        if fav > 0.01 and mfe_first_idx is None:
            mfe_first_idx = i
        if adv > 0.01 and mae_first_idx is None:
            mae_first_idx = i
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        if bar["low"] <= stop and outcome == "open":
            outcome = "stop"
            exit_bar = bar["close_time"].isoformat()
            break
        if bar["high"] >= tp and outcome == "open":
            outcome = "tp"
            exit_bar = bar["close_time"].isoformat()
            break
    if outcome == "open":
        outcome = "horizon"
        exit_bar = bars[end - 1]["close_time"].isoformat() if end > entry_idx + 1 else None
    mae_before_mfe = mae_first_idx is not None and (
        mfe_first_idx is None or mae_first_idx < mfe_first_idx
    )
    return {
        "outcome": outcome,
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
        "mae_before_mfe": mae_before_mfe,
        "exit_bar": exit_bar,
    }


def long_trade_detail(bars: list[dict], entry_idx: int, entry: float, stop: float, tp: float) -> dict:
    base = forward_outcome_long(bars, entry_idx, entry, stop, tp)
    outcome = base["outcome"]
    first_hit = "none"
    exit_time = base.get("exit_bar")
    exit_price = None
    same_bar_both = False

    end = min(len(bars), entry_idx + 1 + HORIZON_BARS)
    for i in range(entry_idx + 1, end):
        bar = bars[i]
        stop_hit = bar["low"] <= stop
        tp_hit = bar["high"] >= tp
        if stop_hit and tp_hit:
            first_hit = "SL"
            outcome = "stop"
            same_bar_both = True
            exit_time = bar["close_time"].isoformat()
            exit_price = stop
            break
        if stop_hit:
            first_hit = "SL"
            outcome = "stop"
            exit_time = bar["close_time"].isoformat()
            exit_price = stop
            break
        if tp_hit:
            first_hit = "TP"
            outcome = "tp"
            exit_time = bar["close_time"].isoformat()
            exit_price = tp
            break

    if first_hit == "none" and outcome == "horizon" and end > entry_idx + 1:
        exit_time = bars[end - 1]["close_time"].isoformat()
        exit_price = float(bars[end - 1]["close"])

    risk_pct = (entry - stop) / entry * 100.0 if entry > 0 else None
    pnl_pct = None
    if first_hit == "SL" and exit_price is not None:
        pnl_pct = (exit_price - entry) / entry * 100.0
    elif first_hit == "TP" and exit_price is not None:
        pnl_pct = (exit_price - entry) / entry * 100.0

    return {
        **base,
        "outcome": outcome,
        "first_hit": first_hit,
        "same_bar_sl_tp": same_bar_both,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
        "risk_pct": round(risk_pct, 4) if risk_pct is not None else None,
        "result_status": "unresolved" if first_hit == "none" else first_hit,
    }


def enrich_long_signals(bars15: list[dict], rows: list[dict]) -> list[dict]:
    from datetime import datetime

    out: list[dict] = []
    for row in rows:
        entry = float(row["close"])
        stop = float(row["stop_price"])
        tp = float(row["tp_bottom"])
        ot = datetime.fromisoformat(row["entry_open"])
        bar = next(b for b in bars15 if b["open_time"] == ot)
        idx = bar["candle_index"]
        detail = long_trade_detail(bars15, idx, entry, stop, tp)
        stop_dist = (entry - stop) / entry * 100.0 if entry > 0 else None
        tp_dist = (tp - entry) / entry * 100.0 if entry > 0 else None
        out.append(
            {
                **row,
                "entry_time": row["entry_open"],
                "entry_price": entry,
                "stop": stop,
                "tp": tp,
                "stop_distance_pct": round(stop_dist, 4) if stop_dist is not None else None,
                "tp_distance_pct": round(tp_dist, 4) if tp_dist is not None else None,
                "first_hit": detail["first_hit"],
                "outcome_label": detail["first_hit"],
                "pnl_pct": detail["pnl_pct"],
                "mae_pct": detail["mae_pct"],
                "mfe_pct": detail["mfe_pct"],
                "exit_time": detail.get("exit_time"),
            }
        )
    return out
