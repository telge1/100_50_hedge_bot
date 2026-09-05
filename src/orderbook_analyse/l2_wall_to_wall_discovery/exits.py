"""Exit variants and path metrics (discovery only)."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.l2_wall_to_wall_discovery import FRONT_RUN_BPS, NOTIONAL_USDT
from orderbook_analyse.l2_wall_to_wall_discovery.models import (
    bps_between,
    samples_between,
    side_adjusted_return_bps,
)
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def _path_mfe_mae(mids: list[float], mid0: float, position_side: str) -> tuple[float | None, float | None]:
    if not mids or mid0 <= 0:
        return None, None
    rets = []
    for m in mids:
        r = side_adjusted_return_bps(mid0, m, position_side)
        if r is not None:
            rets.append(r)
    if not rets:
        return None, None
    return max(rets), min(rets)


def compute_path_and_exits(
    entry: dict[str, Any],
    target: dict[str, Any],
    target_res: dict[str, Any],
    timeline: list[dict[str, Any]],
    samples: list[SampleRow],
    ts_index: list[int],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entry_at = int(entry["entry_at"])
    mid0 = float(entry["entry_mid"])
    pos = entry["position_side"]
    tpx = target.get("target_price_at_entry")
    reached = bool(target_res.get("target_reached"))
    reached_at = target_res.get("target_reached_at")

    path = samples_between(samples, ts_index, entry_at, entry_at + 14_400_000)
    mids = [s.mid for s in path]
    mfe, mae = _path_mfe_mae(mids, mid0, pos)

    progress = None
    if tpx and mid0 > 0:
        full = abs(float(tpx) - mid0)
        if full > 0 and mids:
            if pos == "LONG":
                best = max(mids)
                progress = max(0.0, min(1.0, (best - mid0) / full))
            else:
                best = min(mids)
                progress = max(0.0, min(1.0, (mid0 - best) / full))

    path_row = {
        "signal_id": entry["signal_id"],
        "module": entry["module"],
        "variant": entry["variant"],
        "symbol": entry["symbol"],
        "position_side": pos,
        "target_reached": reached,
        "time_to_target_ms": (int(reached_at) - entry_at) if reached_at else None,
        "max_progress_to_target": progress,
        "mfe_bps_before_target": mfe,
        "mae_bps_before_target": mae,
        "target_still_present_at_contact": None,
        "target_end_state": target_res.get("target_end_state"),
    }
    if reached and timeline:
        # last state at reach
        at_reach = [t for t in timeline if t["ts_ms"] <= int(reached_at)]
        if at_reach:
            path_row["target_still_present_at_contact"] = at_reach[-1].get("target_qty") is not None

    exits: list[dict[str, Any]] = []

    def _exit(variant: str, ts: int | None, reason: str) -> None:
        if ts is None:
            exits.append(
                {
                    "signal_id": entry["signal_id"],
                    "exit_variant": variant,
                    "exit_at": None,
                    "exit_mid": None,
                    "exit_return_bps": None,
                    "gross_pnl_1000": None,
                    "reason": reason,
                    "completed": False,
                }
            )
            return
        # sample at/after ts
        after = samples_between(samples, ts_index, ts - 1, ts + 60_000)
        if not after:
            exits.append(
                {
                    "signal_id": entry["signal_id"],
                    "exit_variant": variant,
                    "exit_at": ts,
                    "exit_mid": None,
                    "exit_return_bps": None,
                    "gross_pnl_1000": None,
                    "reason": reason,
                    "completed": False,
                }
            )
            return
        # prefer first sample >= ts
        s = next((x for x in after if x.ts_ms >= ts), after[0])
        ret = side_adjusted_return_bps(mid0, s.mid, pos)
        pnl = None if ret is None else NOTIONAL_USDT * ret / 10000.0
        exits.append(
            {
                "signal_id": entry["signal_id"],
                "exit_variant": variant,
                "exit_at": s.ts_ms,
                "exit_mid": s.mid,
                "exit_return_bps": ret,
                "gross_pnl_1000": pnl,
                "reason": reason,
                "completed": True,
            }
        )

    # E1 first touch
    _exit("E1_TARGET_FIRST_TOUCH", int(reached_at) if reached_at else None, "target_first_touch")

    # E2 front-run: first time within FRONT_RUN_BPS of target
    fr_ts = None
    if tpx:
        for s in path:
            d = bps_between(s.mid, float(tpx), mid0)
            if d is not None and d <= FRONT_RUN_BPS:
                fr_ts = s.ts_ms
                break
            if reached_at and s.ts_ms >= int(reached_at):
                break
    _exit("E2_FRONT_RUN_TARGET", fr_ts, "front_run_buffer")

    # E3 defense confirm
    def_ts = None
    if target_res.get("target_defended") and timeline:
        for t in timeline:
            if t["state"] == "TARGET_DEFENDED":
                def_ts = t["ts_ms"]
                break
    _exit("E3_TARGET_DEFENSE_CONFIRM", def_ts, "target_defense")

    # E4 break continue — exit at path end or next hop placeholder (same as data end after break)
    brk_ts = None
    if target_res.get("target_broken") and timeline:
        for t in timeline:
            if t["state"] == "TARGET_BREAK_CONFIRMED":
                brk_ts = t["ts_ms"]
                break
    # discovery: mark continue; exit mid at +60s after break if available
    cont_ts = (brk_ts + 60_000) if brk_ts else None
    _exit("E4_TARGET_BREAK_CONTINUE", cont_ts, "break_continue_mark")

    # E5 break reclaim exit
    rec_ts = None
    if target_res.get("target_break_reclaimed") and timeline:
        for t in timeline:
            if t["state"] == "TARGET_BREAK_RECLAIMED":
                rec_ts = t["ts_ms"]
                break
    _exit("E5_TARGET_BREAK_RECLAIM_EXIT", rec_ts, "counter_reclaim")

    # E6 invalidation: entry wall reclaimed against position
    inv_ts = None
    entry_wall = float(entry["wall_price"])
    for s in path:
        if pos == "LONG" and s.mid < entry_wall:
            inv_ts = s.ts_ms
            break
        if pos == "SHORT" and s.mid > entry_wall:
            inv_ts = s.ts_ms
            break
    _exit("E6_INVALIDATION_EXIT", inv_ts, "entry_thesis_invalidated")

    return path_row, exits
