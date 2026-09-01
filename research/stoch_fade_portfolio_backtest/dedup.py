from __future__ import annotations

from collections import defaultdict
from typing import Any

from .config import TF_RANK
from .timeutil import parse_ts


def trade_pnl_usdt(pnl_pct_gross: object, notional: float) -> float:
    try:
        pct = float(pnl_pct_gross)
    except (TypeError, ValueError):
        pct = 0.0
    return notional * pct / 100.0


def _tf_rank(tf: object) -> int:
    return int(TF_RANK.get(str(tf), -1))


def winner_key(row: dict[str, Any]) -> tuple:
    return (-_tf_rank(row.get("timeframe")), str(row.get("signal_id") or ""))


def chronological_key(row: dict[str, Any]) -> tuple:
    entry = parse_ts(row.get("entry_time"))
    stamp = entry.timestamp() if entry else 0.0
    return (stamp, -_tf_rank(row.get("timeframe")), str(row.get("symbol") or ""), str(row.get("signal_id") or ""))


def flatten_pair(pair: dict[str, Any]) -> dict[str, Any]:
    s = pair["signal"]
    o = pair["outcome"]
    return {
        "signal_id": str(s.get("signal_id") or o.get("signal_id")),
        "symbol": str(s.get("symbol")),
        "timeframe": str(s.get("timeframe")),
        "direction": str(s.get("direction") or "").upper(),
        "entry_time": s.get("entry_time"),
        "entry_price": s.get("entry_price"),
        "tp_price": s.get("tp_price"),
        "sl_price": s.get("sl_price"),
        "outcome": str(o.get("outcome") or "").upper(),
        "exit_time": o.get("exit_time"),
        "exit_price": o.get("exit_price"),
        "exit_reason": o.get("exit_reason"),
        "duration_seconds": o.get("duration_seconds"),
        "pnl_pct_gross": o.get("pnl_pct_gross"),
    }


def dedup_pairs(pairs: list[dict[str, Any]], notional: float) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    flat = [flatten_pair(p) for p in pairs]
    for row in flat:
        key = (row["symbol"], str(row["entry_time"]))
        groups[key].append(row)

    kept: list[dict[str, Any]] = []
    dropped_rows: list[dict[str, Any]] = []
    conflict_groups = 0
    duplicate_groups = 0
    dropped_pnl = 0.0
    for key, members in groups.items():
        if len(members) == 1:
            kept.append(members[0])
            continue
        duplicate_groups += 1
        directions = {m["direction"] for m in members}
        conflict = len(directions) > 1
        if conflict:
            conflict_groups += 1
        winner = sorted(members, key=winner_key)[0]
        kept.append(winner)
        for m in members:
            if m["signal_id"] == winner["signal_id"]:
                continue
            pnl = trade_pnl_usdt(m.get("pnl_pct_gross"), notional) if m["outcome"] in {"WIN", "LOSS"} else 0.0
            dropped_pnl += pnl
            dropped_rows.append(
                {
                    "duplicate_key": {"symbol": key[0], "entry_time": key[1]},
                    "kept_signal_id": winner["signal_id"],
                    "dropped_signal_id": m["signal_id"],
                    "kept_timeframe": winner["timeframe"],
                    "dropped_timeframe": m["timeframe"],
                    "direction": m["direction"],
                    "kept_direction": winner["direction"],
                    "result": m["outcome"],
                    "pnl_pct_gross": m.get("pnl_pct_gross"),
                    "theoretical_pnl_usdt": pnl,
                    "reason": "SAME_SYMBOL_ENTRY_TIME_DUPLICATE",
                    "direction_conflict": conflict,
                    "extra_flag": "DUPLICATE_DIRECTION_CONFLICT" if conflict else None,
                }
            )
    kept.sort(key=chronological_key)
    return {
        "kept": kept,
        "dropped": dropped_rows,
        "stats": {
            "raw_joined": len(flat),
            "duplicate_groups": duplicate_groups,
            "dropped_signals": len(dropped_rows),
            "direction_conflict_groups": conflict_groups,
            "after_dedup": len(kept),
            "theoretical_pnl_usdt_dropped_duplicates": dropped_pnl,
        },
    }
