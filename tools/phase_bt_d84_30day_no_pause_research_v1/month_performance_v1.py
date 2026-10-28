"""Monthly performance aggregation for BT-D84 research replays."""

from __future__ import annotations

from typing import Any


def _trades_from_modeled(modeled: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fills: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    for row in modeled:
        ev = row.get("event")
        pid = str(row.get("position_id") or "")
        if ev == "SIMULATED_FILL":
            fills[pid] = dict(row)
        elif ev in ("SIMULATED_TP", "SIMULATED_SL", "SIMULATED_EXIT_AMBIGUOUS"):
            base = dict(fills.get(pid) or {})
            gross = row.get("gross_return_pct_short")
            trades.append(
                {
                    "position_id": pid,
                    "signal_close_utc": base.get("signal_close_utc") or row.get("signal_close_utc"),
                    "fill_time_utc": base.get("decision_close_utc"),
                    "entry_price": base.get("entry_price") or row.get("entry_price"),
                    "sl_price": base.get("sl_price"),
                    "tp_price": base.get("tp_price"),
                    "exit_time_utc": row.get("decision_close_utc"),
                    "exit_reason": ev,
                    "exit_price": row.get("exit_price"),
                    "gross_return_pct_short": gross,
                    "ambiguous_intrabar": row.get("ambiguous_intrabar"),
                    "pool_key": base.get("pool_key"),
                    "order_id": base.get("order_id") or row.get("order_id"),
                }
            )
    return trades


def summarize_month_replay(
    replay: dict[str, Any],
    *,
    label: str,
    post_sl_pause_enabled: bool,
) -> dict[str, Any]:
    modeled = list(replay.get("modeled_positions") or [])
    trades = _trades_from_modeled(modeled)
    resolved = [t for t in trades if t["exit_reason"] in ("SIMULATED_TP", "SIMULATED_SL")]
    wins = [t for t in resolved if t["exit_reason"] == "SIMULATED_TP"]
    losses = [t for t in resolved if t["exit_reason"] == "SIMULATED_SL"]
    ambiguous = [t for t in trades if t["exit_reason"] == "SIMULATED_EXIT_AMBIGUOUS"]

    win_returns = [float(t["gross_return_pct_short"]) for t in wins if t.get("gross_return_pct_short") is not None]
    loss_returns = [float(t["gross_return_pct_short"]) for t in losses if t.get("gross_return_pct_short") is not None]

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(resolved, key=lambda x: x.get("exit_time_utc") or ""):
        g = t.get("gross_return_pct_short")
        if g is None:
            continue
        cumulative += float(g)
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    max_losing_streak = 0
    streak = 0
    for t in sorted(resolved, key=lambda x: x.get("exit_time_utc") or ""):
        if t["exit_reason"] == "SIMULATED_SL":
            streak += 1
            max_losing_streak = max(max_losing_streak, streak)
        else:
            streak = 0

    open_fills = [
        m
        for m in modeled
        if m.get("event") == "SIMULATED_FILL"
        and not any(
            x.get("position_id") == m.get("position_id") and x.get("event") != "SIMULATED_FILL"
            for x in modeled
        )
    ]

    win_rate = (len(wins) / len(resolved) * 100.0) if resolved else None

    return {
        "label": label,
        "post_sl_pause_enabled": post_sl_pause_enabled,
        "pool_candidates_seen": replay.get("pool_candidates_seen"),
        "ema_authorized": len(replay.get("authorized_candidates") or []),
        "ema_blocked": len(replay.get("blocked_candidates") or []),
        "pending_orders_created": replay.get("pending_orders_created"),
        "pending_cancels_ema": replay.get("pending_cancels_ema"),
        "pending_cancellations_total": len(replay.get("pending_cancel_audit") or []),
        "modeled_fills": replay.get("modeled_fills"),
        "tp_count": len(wins),
        "sl_count": len(losses),
        "ambiguous_count": len(ambiguous),
        "still_open_trades": len(open_fills) if replay.get("position_open_at_end") else 0,
        "position_open_at_end": replay.get("position_open_at_end"),
        "pending_open_at_end": replay.get("pending_open_at_end"),
        "gross_win_rate_pct_resolved": round(win_rate, 4) if win_rate is not None else None,
        "avg_winning_trade_pct": round(sum(win_returns) / len(win_returns), 6) if win_returns else None,
        "avg_losing_trade_pct": round(sum(loss_returns) / len(loss_returns), 6) if loss_returns else None,
        "cumulative_gross_return_pct": round(cumulative, 6),
        "max_losing_streak": max_losing_streak,
        "max_drawdown_pct_under_unit_gross_stack": round(max_dd, 6),
        "drawdown_assumption": "each resolved trade PnL added in chronological order; no fees/leverage",
        "pause_state_final": replay.get("pause_state_final"),
        "pause_events_count": len(replay.get("pause_events") or []),
        "coverage_incomplete": len(replay.get("coverage_incomplete") or []),
        "trade_ledger": trades,
    }


def chart_review_timestamps(replay: dict[str, Any], *, max_events: int = 40) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in replay.get("authorized_candidates") or []:
        out.append(
            {
                "utc": row["decision_close_utc"],
                "kind": "EMA_V3_AUTHORIZED",
                "pool_key": str(row.get("pool_key")),
            }
        )
    for row in replay.get("pending_lifecycle") or []:
        if row.get("event") in ("CREATE", "FILLED", "CANCEL"):
            out.append(
                {
                    "utc": str(row.get("decision_close_utc")),
                    "kind": f"PENDING_{row.get('event')}",
                    "detail": str(row.get("order_id") or row.get("cancel_reason") or ""),
                }
            )
    for row in replay.get("modeled_positions") or []:
        out.append(
            {
                "utc": str(row.get("decision_close_utc")),
                "kind": str(row.get("event")),
                "detail": str(row.get("pool_key") or row.get("exit_price") or ""),
            }
        )
    out.sort(key=lambda x: x["utc"])
    return out[:max_events]
