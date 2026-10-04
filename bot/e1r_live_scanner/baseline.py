"""Incremental baseline pipeline (order matches collect_signals)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bot.e1r_live_scanner.config import ensure_runtime_paths


def process_bar_baseline(
    symbol: str,
    bar: dict,
    *,
    moment: datetime,
    candles15,
    market: dict,
    lld_cfg,
    lld_cache: dict,
    guard,
    by_open: dict,
    seen_no_guard: set[str],
    seen_with_guard: set[str],
    report_from: datetime,
    report_to: datetime,
) -> list[dict]:
    """
    One closed 15m bar. Mirrors analyze_floor_guard_signal_list_v1.collect_signals loop body.
    """
    ensure_runtime_paths()
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index
    from find_short_entry_15m_v1 import down_4h, entry_setup, last_closed_index, row_for

    blocked, reason = guard.advance(bar, moment)
    out: list[dict] = []
    if moment < report_from or moment > report_to:
        return out

    idx = bar["candle_index"]
    pools, _ui, _proof = scanner_pools_for_index(candles15, "15m", idx, lld_cfg, cache=lld_cache)
    setup = entry_setup(pools, bar, moment)
    if setup is None:
        return out
    pool, upper, lower, gap = setup
    h4_index = last_closed_index(market["4h"]["bars"], moment)
    if not down_4h(market["4h"]["bars"], h4_index):
        return out
    h4 = market["4h"]["bars"][h4_index]
    source_t = pool.get("source")
    birth = by_open.get(source_t, bar) if source_t is not None else bar
    row = row_for(symbol, pool, birth, bar, h4, None, None, upper, lower, gap)

    would_new_no = pool["pool_id"] not in seen_no_guard
    would_new_with = pool["pool_id"] not in seen_with_guard and not blocked
    if would_new_no:
        seen_no_guard.add(pool["pool_id"])
        out.append({**row, "variant": "no_guard", "floor_guard_blocked": False, "floor_reason": None})
    if would_new_with:
        seen_with_guard.add(pool["pool_id"])
        out.append({**row, "variant": "with_guard", "floor_guard_blocked": False, "floor_reason": None})
    elif blocked and pool["pool_id"] not in seen_with_guard:
        out.append(
            {
                **row,
                "variant": "floor_blocked",
                "floor_guard_blocked": True,
                "floor_reason": reason,
            }
        )
    return out


def enrich_signal_row(
    row: dict,
    *,
    e1r_eval: dict,
    decision_time: datetime,
    state_ts: dict[str, str] | None = None,
) -> dict[str, Any]:
    variant = row.get("variant")
    if variant == "floor_blocked":
        status = "BLOCKED"
        block_reason = row.get("floor_reason") or "floor_guard"
    elif variant == "with_guard":
        if e1r_eval.get("ignore"):
            status = "BLOCKED"
            block_reason = e1r_eval.get("status", "E1R")
        else:
            status = "ALLOWED"
            block_reason = None
    elif variant == "no_guard":
        status = "BASELINE_NO_GUARD"
        block_reason = None
    else:
        status = "UNKNOWN"
        block_reason = None

    entry_price = float(row.get("close") or row.get("entry_price") or 0)
    stop = row.get("stop_price") or row.get("stop")
    tp = row.get("tp_top") if row.get("tp_top") not in ("", None) else row.get("tp")

    return {
        "symbol": row["symbol"],
        "entry_open": row["entry_open"],
        "decision_time": decision_time.isoformat(),
        "entry_price": entry_price,
        "stop": stop,
        "tp": tp,
        "pool_id": row.get("pool_id"),
        "floor_guard_blocked": bool(row.get("floor_guard_blocked")),
        "floor_reason": row.get("floor_reason"),
        "e1r_state": e1r_eval.get("e1r_state"),
        "signal_status": status,
        "block_reason": block_reason,
        "ema200": e1r_eval.get("ema200"),
        "rising_3": e1r_eval.get("rising_3"),
        "cluster_1_top": e1r_eval.get("cluster_1_top"),
        "cluster_2_top": e1r_eval.get("cluster_2_top"),
        "cluster_3_top": e1r_eval.get("cluster_3_top"),
        "variant": variant,
        "state_ts": state_ts or {},
    }
