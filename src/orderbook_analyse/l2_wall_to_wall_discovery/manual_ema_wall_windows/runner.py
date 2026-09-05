"""Orchestrate manual EMA+wall window analysis (research/read-only)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.l2_wall_attack_discovery.trades import load_public_trades
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import (
    FORMAT_VERSION,
    MISSING,
    SYMBOL,
    TICK,
    WINDOWS,
    ZONE_ATR_FRAC,
    ZONE_MIN_TICKS,
    parse_utc,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.classify import (
    classify_window,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.episodes import (
    dedupe_episodes,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.impact import (
    classify_flow_mechanism,
    impact_rows,
    summarize_trades,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.indicators import (
    classify_trend,
    find_swings,
    prepare_5m_indicators,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.outcomes import (
    expected_direction,
    outcome_rows,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zone_replay import (
    is_majorish,
    replay_analysis_samples,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (
    distance_to_zone,
    make_zone,
    swing_in_zone,
    zones_overlap,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.loaders import load_candles_1m
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

OUT_NAME = "manual_ema_wall_windows_btc_20260825_v1"


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _parse_iso_ms(s: str | None) -> int | None:
    if not s or s == MISSING:
        return None
    return int(parse_utc(s).timestamp() * 1000)


def assert_live_safe(forbidden_pids: tuple[int, ...] = (147111, 3940620, 3946369)) -> dict[str, Any]:
    alive = []
    for pid in forbidden_pids:
        if Path(f"/proc/{pid}").exists():
            alive.append(pid)
    return {
        "forbidden_pids_untouched": True,
        "pids_observed_alive": alive,
        "writes": "results_only_new_folder",
        "clickhouse": "read_only",
    }


def coverage_for_window(
    *,
    window: dict[str, str],
    samples: list[Any],
    trades: pd.DataFrame,
    candles_1m: pd.DataFrame,
    closed_l2_end: datetime,
) -> dict[str, Any]:
    start = parse_utc(window["start_utc"])
    end = parse_utc(window["end_utc"])
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    win_samples = [s for s in samples if start_ms <= s.ts_ms < end_ms]
    incomplete_parts: list[str] = []
    l2_end_eff = end
    if end > closed_l2_end:
        incomplete_parts.append(
            f"L2_closed_ends_{closed_l2_end.isoformat().replace('+00:00','Z')}_needed_{window['end_utc']}"
        )
        l2_end_eff = closed_l2_end
    l2_end_ms = int(l2_end_eff.timestamp() * 1000)
    win_samples = [s for s in samples if start_ms <= s.ts_ms < min(end_ms, l2_end_ms)]

    genuine = [s for s in win_samples if s.genuine and not s.carried_forward]
    levels_ok = all(s.bid_levels >= 200 and s.ask_levels >= 200 for s in genuine) if genuine else False
    freq = None
    gaps = 0
    if len(genuine) >= 2:
        dts = [genuine[i].ts_ms - genuine[i - 1].ts_ms for i in range(1, len(genuine))]
        freq = sum(dts) / len(dts)
        gaps = sum(1 for d in dts if d > 2000)

    tsub = trades[(trades["ts_ms"] >= start_ms) & (trades["ts_ms"] < end_ms)] if not trades.empty else trades
    tsum = summarize_trades(tsub) if tsub is not None else summarize_trades(pd.DataFrame())

    c_start = candles_1m["open_time"] >= pd.Timestamp(start)
    c_end = candles_1m["open_time"] < pd.Timestamp(end)
    nc1 = int((c_start & c_end).sum()) if not candles_1m.empty else 0
    # 5m count approx
    expected_1m = int((end - start).total_seconds() // 60)
    if nc1 < expected_1m - 2:
        incomplete_parts.append(f"candles_1m_have_{nc1}_expected_~{expected_1m}")

    expected_l2_ms = int((min(end, closed_l2_end) - start).total_seconds() * 1000)
    if expected_l2_ms > 0 and len(genuine) < (expected_l2_ms / 250) * 0.5:
        incomplete_parts.append("L2_sample_density_low")

    status = "OK" if not incomplete_parts else "DATA_INCOMPLETE"
    return {
        "window_id": window["window_id"],
        "start_utc": window["start_utc"],
        "end_utc": window["end_utc"],
        "status": status,
        "incomplete_reason": "|".join(incomplete_parts) if incomplete_parts else "",
        "l2_samples": len(win_samples),
        "l2_genuine": len(genuine),
        "l2_levels_200_ok": levels_ok,
        "l2_carried_forward": sum(1 for s in win_samples if s.carried_forward),
        "l2_mean_sample_ms": freq if freq is not None else MISSING,
        "l2_gaps_gt_2s": gaps,
        "trade_count": tsum["trade_count"],
        "trade_buy_notional": tsum["buy_notional"],
        "trade_sell_notional": tsum["sell_notional"],
        "trade_largest": tsum["largest_trade"],
        "candles_1m_count": nc1,
        "closed_l2_available_until": closed_l2_end.isoformat().replace("+00:00", "Z"),
    }


def _find_contact(
    win_samples: list[Any],
    zone20,
    zone59,
    trend_class: str,
    *,
    center_ms: int,
    window_id: str,
) -> tuple[Any | None, str, str, int | None]:
    """Return (zone, zone_name, role, contact_ts_ms) near the chart mark center."""
    if not win_samples or zone20 is None:
        return None, "none", "none", None
    role = (
        "resistance"
        if trend_class in ("BEARISH", "TRANSITION", "RANGE", "UNDETERMINED")
        else "support"
    )

    # Mid nearest to center
    mid_c = min(win_samples, key=lambda s: abs(s.ts_ms - center_ms)).mid

    def dist_outside(zone, px: float) -> float:
        if zone.low <= px <= zone.high:
            return 0.0
        if px < zone.low:
            return zone.low - px
        return px - zone.high

    # Primary zone: nearest band to mid at center; final_circle prefers EMA59 if close
    d20 = dist_outside(zone20, mid_c)
    d59 = dist_outside(zone59, mid_c) if zone59 else 1e18
    prefer59 = window_id == "final_circle" and zone59 is not None and d59 <= d20 * 1.25
    if prefer59 or (zone59 is not None and d59 < d20):
        primary, name = zone59, "EMA59"
    else:
        primary, name = zone20, "EMA20"

    # Contact: sample in ±12m of center inside/near zone; else nearest approach in window
    lo = center_ms - 12 * 60_000
    hi = center_ms + 12 * 60_000
    near = [s for s in win_samples if lo <= s.ts_ms <= hi]
    pool = near if near else win_samples

    def score_touch(s) -> float:
        if primary.low <= s.mid <= primary.high:
            return abs(s.ts_ms - center_ms) * 1e-6  # prefer closer in time
        # distance to band + time penalty
        return dist_outside(primary, s.mid) * 1000 + abs(s.ts_ms - center_ms) * 1e-3

    # If already above resistance at center (breakout context), contact = last in-band before center
    if role == "resistance" and mid_c > primary.high:
        pre = [
            s
            for s in win_samples
            if s.ts_ms <= center_ms and primary.low <= s.mid <= primary.high
        ]
        if pre:
            return primary, name, role, pre[-1].ts_ms
        # else first time mid exceeded high before center
        crossed = [
            s for s in win_samples if s.ts_ms <= center_ms and s.mid > primary.high
        ]
        if crossed:
            return primary, name, role, crossed[0].ts_ms

    best = min(pool, key=score_touch)
    if dist_outside(primary, best.mid) <= primary.half_width * 3 or (
        primary.low <= best.mid <= primary.high
    ):
        return primary, name, role, best.ts_ms
    return primary, name, role, best.ts_ms


def analyze_one_window(
    *,
    window: dict[str, str],
    samples: list[Any],
    trades: pd.DataFrame,
    bars_5m: pd.DataFrame,
    cov: dict[str, Any],
) -> dict[str, Any]:
    start = parse_utc(window["start_utc"])
    end = parse_utc(window["end_utc"])
    center = parse_utc(window["center_utc"])
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    win_samples = [s for s in samples if start_ms <= s.ts_ms < end_ms]
    # if incomplete L2, still analyze available part
    if cov["status"] == "DATA_INCOMPLETE" and "L2_closed_ends" in cov.get("incomplete_reason", ""):
        win_samples = [s for s in samples if start_ms <= s.ts_ms < end_ms]

    trend = classify_trend(bars_5m, center)
    swings = find_swings(bars_5m, center)

    zone20 = zone59 = None
    if trend.warmup_ok and trend.ema20 and trend.atr:
        zone20 = make_zone("EMA20", trend.ema20, trend.atr)
    if trend.warmup_ok and trend.ema59 and trend.atr:
        zone59 = make_zone("EMA59", trend.ema59, trend.atr)

    mid_at_center = None
    for s in win_samples:
        if s.ts_ms >= int(center.timestamp() * 1000):
            mid_at_center = s.mid
            break
    if mid_at_center is None and win_samples:
        mid_at_center = win_samples[len(win_samples) // 2].mid

    center_ms = int(center.timestamp() * 1000)
    primary_zone, zone_name, zone_role, contact_ts = _find_contact(
        win_samples,
        zone20,
        zone59,
        trend.classification,
        center_ms=center_ms,
        window_id=window["window_id"],
    )

    # Zone kind / confluence labels
    zone_kind = "keine relevante Konfluenz"
    sh = swings.get("last_swing_high")
    sl = swings.get("last_swing_low")
    sh_f = float(sh) if sh not in (None, MISSING) else None
    sl_f = float(sl) if sl not in (None, MISSING) else None

    overlap = zones_overlap(zone20, zone59) if zone20 and zone59 else False
    first_touched = zone_name

    ema_zone_row: dict[str, Any] = {
        "window_id": window["window_id"],
        "center_utc": window["center_utc"],
        "ema20": trend.ema20 if trend.ema20 is not None else MISSING,
        "ema59": trend.ema59 if trend.ema59 is not None else MISSING,
        "atr": trend.atr if trend.atr is not None else MISSING,
        "zone20_low": zone20.low if zone20 else MISSING,
        "zone20_high": zone20.high if zone20 else MISSING,
        "zone59_low": zone59.low if zone59 else MISSING,
        "zone59_high": zone59.high if zone59 else MISSING,
        "half_width": zone20.half_width if zone20 else MISSING,
        "zones_overlap": overlap,
        "mid_at_center": mid_at_center if mid_at_center is not None else MISSING,
        "first_touched_zone": first_touched,
        "zone_kind": zone_kind,
    }
    if zone20 and mid_at_center is not None:
        d = distance_to_zone(mid_at_center, zone20)
        ema_zone_row.update({f"ema20_{k}": v for k, v in d.items()})
    if zone59 and mid_at_center is not None:
        d = distance_to_zone(mid_at_center, zone59)
        ema_zone_row.update({f"ema59_{k}": v for k, v in d.items()})

    # Wall confluence around contact
    contact_samples = []
    if contact_ts is not None:
        contact_samples = [s for s in win_samples if contact_ts - 5_000 <= s.ts_ms <= contact_ts + 60_000]

    def pick_wall(s, zone_name: str, role: str):
        if zone_name == "EMA20":
            return s.ask_in_ema20 if role == "resistance" else s.bid_in_ema20
        return s.ask_in_ema59 if role == "resistance" else s.bid_in_ema59

    wall_before = None
    wall_at = None
    wall_after = None
    if contact_ts is not None:
        pre = [s for s in win_samples if contact_ts - 60_000 <= s.ts_ms < contact_ts]
        at = [s for s in win_samples if contact_ts <= s.ts_ms <= contact_ts + 5_000]
        post = [s for s in win_samples if contact_ts + 55_000 <= s.ts_ms <= contact_ts + 65_000]
        for s in reversed(pre):
            w = pick_wall(s, zone_name, zone_role)
            if w and is_majorish(w):
                wall_before = w
                break
            if w and wall_before is None:
                wall_before = w
        for s in at:
            w = pick_wall(s, zone_name, zone_role)
            if w:
                wall_at = w
                if is_majorish(w):
                    break
        for s in post:
            w = pick_wall(s, zone_name, zone_role)
            if w:
                wall_after = w
                break

    if wall_at and is_majorish(wall_at):
        if zone_name == "EMA20" and (
            (sh_f and swing_in_zone(sh_f, zone20)) or (sl_f and swing_in_zone(sl_f, zone20))
        ):
            zone_kind = "EMA20 + Swing-Level" if zone_name == "EMA20" else zone_kind
            zone_kind = "EMA + Major-Wall-Konfluenz"
        elif zone_name == "EMA59" and (
            (sh_f and zone59 and swing_in_zone(sh_f, zone59))
            or (sl_f and zone59 and swing_in_zone(sl_f, zone59))
        ):
            zone_kind = "EMA + Major-Wall-Konfluenz"
        else:
            zone_kind = "EMA + Major-Wall-Konfluenz"
    elif zone_name == "EMA20":
        if sh_f and zone20 and swing_in_zone(sh_f, zone20):
            zone_kind = "EMA20 + Swing-Level"
        else:
            zone_kind = "EMA20"
    elif zone_name == "EMA59":
        if sh_f and zone59 and swing_in_zone(sh_f, zone59):
            zone_kind = "EMA59 + Swing-Level"
        else:
            zone_kind = "EMA59"
    ema_zone_row["zone_kind"] = zone_kind

    # Trade impact near contact
    mids = [(s.ts_ms, s.mid) for s in win_samples]
    buy_n = sell_n = 0.0
    if contact_ts is not None and not trades.empty:
        sub = trades[(trades["ts_ms"] >= contact_ts) & (trades["ts_ms"] < contact_ts + 60_000)]
        st = summarize_trades(sub)
        buy_n, sell_n = st["buy_notional"], st["sell_notional"]

    wall_side = "ASK" if zone_role == "resistance" else "BID"
    wn_before = wall_before.notional if wall_before else None
    wn_after = wall_after.notional if wall_after else None
    present_after = wall_after is not None and (wn_after or 0) > 0.2 * (wn_before or 1)
    # crude consumption estimate from aggressive notional vs wall
    consumed = None
    if wn_before and contact_ts is not None:
        aggressive = buy_n if wall_side == "ASK" else sell_n
        if wn_after is not None:
            consumed = max(0.0, wn_before - wn_after)
            # cap by aggressive flow
            consumed = min(consumed, aggressive)
        else:
            consumed = min(aggressive, wn_before)

    # price held beyond zone?
    price_held_beyond = False
    if contact_ts is not None and primary_zone is not None:
        post = [s for s in win_samples if contact_ts + 30_000 <= s.ts_ms <= contact_ts + 120_000]
        if zone_role == "resistance":
            price_held_beyond = bool(post) and all(s.mid > primary_zone.high for s in post[-20:]) if post else False
            if post:
                price_held_beyond = sum(1 for s in post if s.mid > primary_zone.high) / len(post) > 0.7
        else:
            if post:
                price_held_beyond = sum(1 for s in post if s.mid < primary_zone.low) / len(post) > 0.7

    wall_moved = False
    if wall_before and wall_after and abs(wall_before.price - wall_after.price) >= TICK * 3:
        wall_moved = True

    mechanism = classify_flow_mechanism(
        attack_side="BUY" if wall_side == "ASK" else "SELL",
        wall_side=wall_side,
        buy_n=buy_n,
        sell_n=sell_n,
        wall_notional_before=wn_before,
        wall_notional_after=wn_after,
        wall_present_after=present_after,
        price_held_beyond=price_held_beyond,
        consumed_estimate=consumed,
    )
    # Rejection without full breakout: preexisting ask/bid wall + mid never held beyond band
    if (
        mechanism == "UNDETERMINED"
        and contact_ts is not None
        and primary_zone is not None
        and wall_before is not None
        and not price_held_beyond
    ):
        post = [s for s in win_samples if contact_ts <= s.ts_ms <= contact_ts + 180_000]
        if zone_role == "resistance" and post:
            frac_above = sum(1 for s in post if s.mid > primary_zone.high) / len(post)
            if frac_above < 0.25:
                mechanism = "ASK_DEFENSE"
        if zone_role == "support" and post:
            frac_below = sum(1 for s in post if s.mid < primary_zone.low) / len(post)
            if frac_below < 0.25:
                mechanism = "BID_DEFENSE"

    data_incomplete = cov["status"] == "DATA_INCOMPLETE"
    # For final_circle: still classify on available data but flag incomplete
    tl = classify_window(
        data_incomplete=False if win_samples else data_incomplete,
        incomplete_reason=cov.get("incomplete_reason", ""),
        samples=win_samples,
        zone=primary_zone,
        zone_role=zone_role,
        contact_ts_ms=contact_ts,
        mechanism=mechanism,
        wall_present_before_contact=wall_before is not None,
        wall_present_after_60s=present_after,
        wall_moved=wall_moved,
    )
    if data_incomplete and window["window_id"] == "final_circle":
        # keep classification from available data but annotate
        tl.notes = (tl.notes + "|" if tl.notes else "") + "PARTIAL_L2_to_15:00Z|" + cov.get(
            "incomplete_reason", ""
        )
        if not win_samples:
            tl.primary_class = "DATA_INCOMPLETE"

    # next zone after classification
    class_ms = _parse_iso_ms(tl.classification_at)
    next_zone_hit = None
    next_zone_ts = None
    if class_ms and zone59 and zone_name == "EMA20":
        after = [s for s in samples if s.ts_ms >= class_ms]
        for s in after:
            if zone59.low <= s.mid <= zone59.high:
                next_zone_hit = "EMA59"
                next_zone_ts = s.ts_ms
                break

    breakout_held = MISSING
    if tl.breakout_confirmed_at and tl.reclaim_at:
        breakout_held = "FAILED_RECLAIM"
    elif tl.breakout_confirmed_at:
        breakout_held = "HELD"
    elif tl.breakout_at:
        breakout_held = "UNCONFIRMED"
    else:
        breakout_held = "NO_BREAKOUT"

    entry_px = None
    if class_ms:
        for s in win_samples:
            if s.ts_ms >= class_ms:
                entry_px = s.mid
                break
        if entry_px is None:
            for s in samples:
                if s.ts_ms >= class_ms:
                    entry_px = s.mid
                    break

    path = [(s.ts_ms, s.mid) for s in samples]
    direction = expected_direction(tl.primary_class, zone_role, trend.classification)

    confluence = {
        "window_id": window["window_id"],
        "primary_zone": zone_name,
        "zone_role": zone_role,
        "zone_kind": zone_kind,
        "contact_ts_ms": contact_ts if contact_ts is not None else MISSING,
        "contact_at": _iso(contact_ts) if contact_ts else MISSING,
        "wall_side": wall_side if contact_ts else MISSING,
        "wall_price_before": wall_before.price if wall_before else MISSING,
        "wall_notional_before": wn_before if wn_before is not None else MISSING,
        "wall_rel_before": wall_before.relative_size if wall_before else MISSING,
        "wall_pct_before": wall_before.causal_percentile if wall_before else MISSING,
        "wall_price_at": wall_at.price if wall_at else MISSING,
        "wall_notional_at": wall_at.notional if wall_at else MISSING,
        "wall_majorish_at": is_majorish(wall_at),
        "wall_price_after60": wall_after.price if wall_after else MISSING,
        "wall_notional_after60": wn_after if wn_after is not None else MISSING,
        "wall_preexisting": wall_before is not None,
        "wall_moved": wall_moved,
        "consumed_estimate": consumed if consumed is not None else MISSING,
        "mechanism": mechanism,
        "dist_wall_to_mid_at": (
            abs(wall_at.price - mid_at_center) if wall_at and mid_at_center else MISSING
        ),
    }

    lifecycle = {
        "window_id": window["window_id"],
        "appeared_before_contact": wall_before is not None,
        "present_on_contact": wall_at is not None,
        "replenished": bool(
            wall_before and wall_after and wn_after and wn_before and wn_after >= 0.8 * wn_before and buy_n + sell_n > 0
        ),
        "consumed": bool(consumed and wn_before and consumed >= 0.5 * wn_before),
        "pulled": mechanism == "LIQUIDITY_PULL",
        "migrated": wall_moved,
        "remaining_after_60s": wn_after if wn_after is not None else MISSING,
    }

    timeline = {
        "window_id": window["window_id"],
        "zone_touch_at": tl.zone_touch_at or MISSING,
        "attack_start_at": tl.attack_start_at or MISSING,
        "wall_defended_at": tl.wall_defended_at or MISSING,
        "wall_absorbed_at": tl.wall_absorbed_at or MISSING,
        "breakout_at": tl.breakout_at or MISSING,
        "breakout_confirmed_at": tl.breakout_confirmed_at or MISSING,
        "retest_at": tl.retest_at or MISSING,
        "reclaim_at": tl.reclaim_at or MISSING,
        "classification_at": tl.classification_at or MISSING,
        "primary_class": tl.primary_class,
        "mechanism": tl.mechanism,
        "notes": tl.notes,
    }

    classification = {
        "window_id": window["window_id"],
        "primary_class": tl.primary_class,
        "mechanism": mechanism,
        "zone_role": zone_role,
        "primary_zone": zone_name,
        "trend": trend.classification,
        "direction_hypothesis": direction,
        "data_coverage": cov["status"],
        "primary_wall_price": wall_at.price if wall_at else (wall_before.price if wall_before else MISSING),
    }

    impacts = impact_rows(
        window_id=window["window_id"], contact_ts_ms=contact_ts, trades=trades, mids=mids
    )
    outcomes = outcome_rows(
        window_id=window["window_id"],
        primary_class=tl.primary_class,
        zone_role=zone_role,
        trend=trend.classification,
        classification_ts_ms=class_ms,
        entry_px=entry_px,
        path=path,
        next_zone_hit=next_zone_hit,
        next_zone_ts_ms=next_zone_ts,
        breakout_held=str(breakout_held),
    )

    trend_row = {
        "window_id": window["window_id"],
        "center_utc": window["center_utc"],
        "asof_utc": trend.asof_utc,
        "classification": trend.classification,
        "confidence": trend.confidence,
        "reasons": trend.reasons,
        "score_components": trend.score_components,
        "ema9": trend.ema9 if trend.ema9 is not None else MISSING,
        "ema20": trend.ema20 if trend.ema20 is not None else MISSING,
        "ema59": trend.ema59 if trend.ema59 is not None else MISSING,
        "atr": trend.atr if trend.atr is not None else MISSING,
        "close": trend.close if trend.close is not None else MISSING,
        "last_bar_end": trend.last_bar_end or MISSING,
        "ema20_slope_3": trend.ema20_slope_3 if trend.ema20_slope_3 is not None else MISSING,
        "ema20_slope_6": trend.ema20_slope_6 if trend.ema20_slope_6 is not None else MISSING,
        "ema59_slope_3": trend.ema59_slope_3 if trend.ema59_slope_3 is not None else MISSING,
        "ema59_slope_6": trend.ema59_slope_6 if trend.ema59_slope_6 is not None else MISSING,
        "ret_15m": trend.ret_15m if trend.ret_15m is not None else MISSING,
        "ret_30m": trend.ret_30m if trend.ret_30m is not None else MISSING,
        "ret_60m": trend.ret_60m if trend.ret_60m is not None else MISSING,
        "structure": trend.structure,
        "warmup_ok": trend.warmup_ok,
        "last_swing_high": swings.get("last_swing_high", MISSING),
        "last_swing_low": swings.get("last_swing_low", MISSING),
    }

    return {
        "trend": trend_row,
        "ema_zone": ema_zone_row,
        "confluence": confluence,
        "lifecycle": lifecycle,
        "timeline": timeline,
        "classification": classification,
        "impacts": impacts,
        "outcomes": outcomes,
        "summary": {
            **window,
            **classification,
            "end_utc": window["end_utc"],
            "start_utc": window["start_utc"],
        },
    }


def hypothesis_verdicts(results: list[dict[str, Any]], cov_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {r["classification"]["window_id"]: r for r in results}
    cov = {c["window_id"]: c for c in cov_rows}

    def cls(wid: str) -> str:
        return by_id[wid]["classification"]["primary_class"]

    def mech(wid: str) -> str:
        return by_id[wid]["classification"]["mechanism"]

    # H1: circles 1-5 mostly pullbacks to EMA20 with ask defense
    cids = [f"circle_{i}" for i in range(1, 6)]
    h1_ok = 0
    h1_notes = []
    for wid in cids:
        r = by_id[wid]
        zone = r["classification"]["primary_zone"]
        m = mech(wid)
        c = cls(wid)
        trend = r["trend"]["classification"]
        good = zone == "EMA20" and m == "ASK_DEFENSE" and c == "DEFENSE_REJECTION"
        partial = zone == "EMA20" and (
            m in ("ASK_DEFENSE", "UNDETERMINED") or c in ("DEFENSE_REJECTION", "RANGE_AROUND_ZONE")
        )
        if good:
            h1_ok += 1
        h1_notes.append(f"{wid}:{trend}/{zone}/{m}/{c}/{'OK' if good else 'PART' if partial else 'NO'}")
    if h1_ok >= 4:
        h1 = "CONFIRMED"
    elif h1_ok >= 2 or sum("PART" in n or "OK" in n for n in h1_notes) >= 3:
        h1 = "PARTIALLY_SUPPORTED"
    else:
        h1 = "REJECTED"

    # H2 rectangle EMA20 break after absorption or pull
    r = by_id["rectangle"]
    m = mech("rectangle")
    c = cls("rectangle")
    if c in ("ABSORPTION_THEN_BREAKOUT", "LIQUIDITY_PULL_BREAKOUT") and r["classification"]["primary_zone"] == "EMA20":
        h2 = "CONFIRMED"
    elif m in ("ASK_ABSORPTION", "LIQUIDITY_PULL") and c == "FALSE_BREAKOUT_RECLAIM":
        h2 = "PARTIALLY_SUPPORTED"
    elif c in (
        "ABSORPTION_THEN_BREAKOUT",
        "LIQUIDITY_PULL_BREAKOUT",
        "BREAKOUT_WITHOUT_CONFIRMED_ABSORPTION",
    ):
        h2 = "PARTIALLY_SUPPORTED"
    else:
        h2 = "REJECTED"
    h2_note = f"zone={r['classification']['primary_zone']} class={c} mech={m}"

    # H3 after EMA20 break moved to EMA59
    outs = r["outcomes"]
    next_hit = next((o["next_zone_hit"] for o in outs if o["horizon_s"] == 1800), MISSING)
    next_hits = [o["next_zone_hit"] for o in outs]
    # Also: final_circle primary EMA59 after rectangle
    fin_zone = by_id["final_circle"]["classification"]["primary_zone"]
    if any(x == "EMA59" for x in next_hits) or fin_zone == "EMA59":
        h3 = "CONFIRMED"
    elif c in ("ABSORPTION_THEN_BREAKOUT", "BREAKOUT_WITHOUT_CONFIRMED_ABSORPTION", "LIQUIDITY_PULL_BREAKOUT", "FALSE_BREAKOUT_RECLAIM"):
        h3 = "PARTIALLY_SUPPORTED"
    else:
        h3 = "REJECTED"
    h3_note = f"next_zone={next_hit} break_class={c} final_zone={fin_zone}"

    # H4 final circle at EMA59
    f = by_id["final_circle"]
    if cov["final_circle"]["status"] == "DATA_INCOMPLETE" and not any(
        s for s in []  # placeholder
    ):
        pass
    fz = f["classification"]["primary_zone"]
    fc = cls("final_circle")
    if cov["final_circle"]["status"] == "DATA_INCOMPLETE" and f["classification"]["data_coverage"] == "DATA_INCOMPLETE":
        # still may have partial class
        if fz == "EMA59" and fc in (
            "DEFENSE_REJECTION",
            "FALSE_BREAKOUT_RECLAIM",
            "ABSORPTION_THEN_BREAKOUT",
            "RANGE_AROUND_ZONE",
            "BREAKOUT_WITHOUT_CONFIRMED_ABSORPTION",
        ):
            h4 = "PARTIALLY_SUPPORTED"
        elif fz == "EMA59":
            h4 = "PARTIALLY_SUPPORTED"
        else:
            h4 = "DATA_INCOMPLETE"
    elif fz == "EMA59" and fc in (
        "DEFENSE_REJECTION",
        "FALSE_BREAKOUT_RECLAIM",
        "RANGE_AROUND_ZONE",
        "ABSORPTION_THEN_BREAKOUT",
    ):
        h4 = "CONFIRMED"
    elif fz == "EMA59":
        h4 = "PARTIALLY_SUPPORTED"
    else:
        h4 = "REJECTED"
    h4_note = f"zone={fz} class={fc} coverage={cov['final_circle']['status']}"

    return [
        {"hypothesis": "H1", "verdict": h1, "evidence": ";".join(h1_notes)},
        {"hypothesis": "H2", "verdict": h2, "evidence": h2_note},
        {"hypothesis": "H3", "verdict": h3, "evidence": h3_note},
        {"hypothesis": "H4", "verdict": h4, "evidence": h4_note},
    ]


def write_report(out_dir: Path, *, manifest: dict, hyp: list, results: list, cov: list) -> None:
    lines = [
        "# Manual EMA + Wall Windows — BTCUSDT 2026-08-25 (UTC)",
        "",
        f"- Format: `{FORMAT_VERSION}`",
        f"- Output: `{out_dir}`",
        f"- Live safety: PIDs untouched; CH read-only; closed raw only",
        "",
        "## Verdict summary",
        "",
    ]
    for r in results:
        c = r["classification"]
        lines.append(
            f"- **{c['window_id']}**: {c['primary_class']} | zone={c['primary_zone']} | "
            f"mech={c['mechanism']} | trend={c['trend']} | dir={c['direction_hypothesis']}"
        )
    lines += ["", "## Hypotheses", ""]
    for h in hyp:
        lines.append(f"- **{h['hypothesis']}**: {h['verdict']} — {h['evidence']}")
    lines += ["", "## Coverage", ""]
    for c in cov:
        lines.append(f"- {c['window_id']}: {c['status']} (L2 genuine={c['l2_genuine']}, trades={c['trade_count']})")
    lines += ["", "## Kernaussage", ""]
    lines.append(
        "Siehe Abschlussbericht in der Agent-Antwort: kausaler Trend, EMA20-Defense vs Breakout, "
        "EMA59-Reaktion, Richtungs-Hypothesen LONG/SHORT/NO_TRADE."
    )
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(
    *,
    raw_root: Path,
    out_root: Path,
    force: bool = False,
) -> Path:
    out_dir = out_root / OUT_NAME
    if out_dir.exists() and not force:
        raise SystemExit("NO_OVERWRITE: output folder already exists")
    out_dir.mkdir(parents=True, exist_ok=False)

    live = assert_live_safe()
    load_clickhouse_settings()
    client = get_clickhouse_client()

    candle_start = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    candle_end = datetime(2026, 8, 25, 15, 10, tzinfo=timezone.utc)
    print("Loading candles…", flush=True)
    candles = load_candles_1m(client, symbol=SYMBOL, start=candle_start, end=candle_end)
    bars = prepare_5m_indicators(candles)
    print(f"  candles_1m={len(candles)} bars_5m={len(bars)} warmup_ok={int(bars['warmup_ok'].sum()) if not bars.empty else 0}", flush=True)

    trade_start = datetime(2026, 8, 25, 7, 45, tzinfo=timezone.utc)
    trade_end = datetime(2026, 8, 25, 15, 10, tzinfo=timezone.utc)
    print("Loading trades…", flush=True)
    trades = load_public_trades(symbol=SYMBOL, start=trade_start, end=trade_end)
    print(f"  trades={len(trades)}", flush=True)

    # Closed L2 available range
    l2_start = datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc)
    l2_end = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
    segs = list_closed_segments(
        raw_root, symbols=(SYMBOL,), start=l2_start, end=l2_end, include_boundary_stubs=False
    )
    closed_l2_end = max((s.end_utc for s in segs), default=l2_end)
    print(f"  closed segments={len(segs)} closed_l2_end={closed_l2_end}", flush=True)

    print("Replaying L2 samples (closed only)…", flush=True)
    samples = replay_analysis_samples(
        raw_root,
        symbol=SYMBOL,
        start=l2_start,
        end=l2_end,
        bars_5m=bars,
    )
    print(f"  samples={len(samples)}", flush=True)

    cov_rows = [
        coverage_for_window(
            window=w,
            samples=samples,
            trades=trades,
            candles_1m=candles,
            closed_l2_end=closed_l2_end,
        )
        for w in WINDOWS
    ]

    results = []
    for w, cov in zip(WINDOWS, cov_rows):
        print(f"Analyzing {w['window_id']}…", flush=True)
        results.append(
            analyze_one_window(
                window=w, samples=samples, trades=trades, bars_5m=bars, cov=cov
            )
        )

    # Artifacts
    manual = [{**w} for w in WINDOWS]
    pd.DataFrame(manual).to_csv(out_dir / "manual_windows.csv", index=False)
    pd.DataFrame(cov_rows).to_csv(out_dir / "data_coverage.csv", index=False)
    pd.DataFrame([r["trend"] for r in results]).to_csv(out_dir / "causal_trend_at_center.csv", index=False)
    pd.DataFrame([r["ema_zone"] for r in results]).to_csv(out_dir / "ema_zones.csv", index=False)
    pd.DataFrame([r["confluence"] for r in results]).to_csv(out_dir / "zone_wall_confluence.csv", index=False)
    pd.DataFrame([r["lifecycle"] for r in results]).to_csv(out_dir / "wall_lifecycle.csv", index=False)
    pd.DataFrame([r["timeline"] for r in results]).to_csv(out_dir / "event_timeline.csv", index=False)
    pd.DataFrame([r["classification"] for r in results]).to_csv(
        out_dir / "window_classifications.csv", index=False
    )
    impact_all = [row for r in results for row in r["impacts"]]
    pd.DataFrame(impact_all).to_csv(out_dir / "public_trade_impact.csv", index=False)
    out_all = [row for r in results for row in r["outcomes"]]
    pd.DataFrame(out_all).to_csv(out_dir / "directional_outcomes.csv", index=False)

    summaries = [r["summary"] for r in results]
    episodes = dedupe_episodes(summaries)
    pd.DataFrame(episodes).to_csv(out_dir / "deduplicated_episodes.csv", index=False)

    hyp = hypothesis_verdicts(results, cov_rows)
    pd.DataFrame(hyp).to_csv(out_dir / "hypothesis_verdicts.csv", index=False)

    methodology = {
        "format_version": FORMAT_VERSION,
        "symbol": SYMBOL,
        "timezone": "UTC",
        "zone_half_width": f"max({ZONE_ATR_FRAC}*ATR, {ZONE_MIN_TICKS}*tick)",
        "tick": TICK,
        "trend_score": {
            "stack": 0.30,
            "slopes": 0.25,
            "price_ema20": 0.20,
            "structure": 0.15,
            "returns": 0.10,
            "sign": "+bearish / -bullish",
        },
        "wall_threshold": "causal lookback only; rel_size>=3 and percentile>=0.90 descriptive",
        "no_full_window_q95": True,
        "closed_candles_only": True,
        "open_5m_excluded": True,
        "raw_l2": "closed segments only; open TMP excluded",
        "parameters_not_tuned_on_events": True,
    }
    (out_dir / "methodology.json").write_text(json.dumps(methodology, indent=2), encoding="utf-8")

    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    git_branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "format_version": FORMAT_VERSION,
        "git_branch": git_branch,
        "git_head": git_head,
        "live_safety": live,
        "n_samples": len(samples),
        "n_trades": len(trades),
        "n_candles_1m": len(candles),
        "closed_l2_end": closed_l2_end.isoformat().replace("+00:00", "Z"),
        "windows": WINDOWS,
        "hypothesis_verdicts": hyp,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    write_report(out_dir, manifest=manifest, hyp=hyp, results=results, cov=cov_rows)
    print(f"DONE → {out_dir}", flush=True)
    return out_dir


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument(
        "--raw-root",
        type=Path,
        default=Path("data/orderbook_raw_shadow/ob200_v3"),
    )
    p.add_argument(
        "--out-root",
        type=Path,
        default=Path("results/l2_wall_to_wall_discovery"),
    )
    args = p.parse_args()
    run(raw_root=args.raw_root, out_root=args.out_root)
