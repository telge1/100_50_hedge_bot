"""Causal pull/consumption features at approach anchors (no outcome lookahead)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.orderbook.historical_break_pull_consumption.trades import (
    aggressor_side_for_direction,
    day_trade_csv_path,
    load_trades_window,
    wall_book_side,
)
from research.orderbook.historical_break_pull_consumption.walls import (
    aggressive_flow_in_window,
    snapshot_wall,
)
from research.orderbook.historical_bybit_replay import (
    HistoricalBybitReplayer,
    ObMessage,
    SequenceStatus,
    day_file_path,
    iter_messages,
)
from research.orderbook.historical_protected_level_pull_break_risk import (
    DEFAULT_OB_ROOT,
    DEFAULT_TRADE_ROOT,
    PRIMARY_ANCHOR_BPS,
    PULL_OFFSETS_S,
    ZONE_BPS,
)
from research.orderbook.historical_protected_level_pull_break_risk.approaches import ApproachEpisode


def _parse_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def choose_anchor(ep: ApproachEpisode) -> tuple[str, int] | None:
    """Prefer 10bps approach; fall back to 25 then 50. Never use first_break."""
    for label, attr in (
        ("approach_10bps", "approach_10bps_ts"),
        ("approach_25bps", "approach_25bps_ts"),
        ("approach_50bps", "approach_50bps_ts"),
    ):
        ms = _parse_ms(getattr(ep, attr))
        if ms is not None:
            return label, ms
    return None


def _nearest_snap(snaps: dict[int, Any], target_ms: int, *, tol_ms: int = 1500) -> Any | None:
    if not snaps:
        return None
    best = min(snaps.keys(), key=lambda t: abs(t - target_ms))
    if abs(best - target_ms) > tol_ms:
        return None
    return snaps[best]


def extract_features_for_day(
    episodes: list[ApproachEpisode],
    *,
    symbol: str,
    date: str,
    ob_root: Path = DEFAULT_OB_ROOT,
    trade_root: Path = DEFAULT_TRADE_ROOT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """One OB pass per symbol-day for all episodes on that day."""
    day_eps = [e for e in episodes if e.symbol == symbol and e.date == date]
    if not day_eps:
        return [], [], []

    anchors: list[tuple[ApproachEpisode, str, int]] = []
    for ep in day_eps:
        a = choose_anchor(ep)
        if a is None:
            continue
        anchors.append((ep, a[0], a[1]))

    if not anchors:
        return [], [], [{"symbol": symbol, "date": date, "data_quality": "NO_ANCHOR"}]

    win_start = min(a[2] for a in anchors) - 30_000
    win_end = max(a[2] for a in anchors) + 90_000

    ob_path = day_file_path(symbol, date, data_root=ob_root)
    trade_path = day_trade_csv_path(trade_root, symbol, date)
    quality_base = {
        "symbol": symbol,
        "date": date,
        "ob_path": str(ob_path),
        "ob_exists": ob_path.exists(),
        "trade_path": str(trade_path) if trade_path else None,
        "trade_exists": bool(trade_path and trade_path.exists()),
    }
    if not ob_path.exists() or not trade_path or not trade_path.exists():
        quality_base["data_quality"] = "DATA_INVALID"
        return [], [], [quality_base]

    trades = load_trades_window(
        trade_path, start_ms=win_start, end_ms=win_end, expected_symbol=symbol
    )
    quality_base["trades_in_window"] = len(trades)

    # targets: for each approach, need snaps at anchor+offset
    targets: dict[int, list[tuple[str, int]]] = {}  # ts -> [(approach_id, offset_s)]
    for ep, _alabel, ams in anchors:
        for off in PULL_OFFSETS_S:
            t = ams + int(off) * 1000
            targets.setdefault(t, []).append((ep.approach_id, off))

    # also denser sampling for pull_start detection: every 250ms from anchor to +60s
    dense_need: dict[str, list[int]] = {}
    for ep, _alabel, ams in anchors:
        dense_need[ep.approach_id] = list(range(ams, ams + 60_001, 250))

    replayer = HistoricalBybitReplayer()
    # store last book state keyed loosely by capturing at message times
    capture: dict[str, dict[int, Any]] = {ep.approach_id: {} for ep, _, _ in anchors}
    ep_by_id = {ep.approach_id: (ep, alabel, ams) for ep, alabel, ams in anchors}

    remaining = sorted(targets.keys())
    dense_remaining = {aid: sorted(ts_list) for aid, ts_list in dense_need.items()}

    for item in iter_messages(ob_path, expected_symbol=symbol, skip_malformed=True):
        if not isinstance(item, ObMessage):
            continue
        msg = item
        if msg.ts_ms > win_end:
            break
        if msg.ts_ms < win_start - 5_000:
            replayer.apply_message(msg)
            continue

        # flush marker targets that are now in the past (use last book)
        while remaining and remaining[0] < msg.ts_ms:
            t = remaining.pop(0)
            if not replayer.book.has_snapshot:
                continue
            for aid, off in targets.get(t, []):
                ep, _, _ = ep_by_id[aid]
                book_side = wall_book_side(ep.direction)
                capture[aid][t] = snapshot_wall(
                    replayer.book, ts_ms=t, level=ep.level, book_side=book_side
                )

        replayer.apply_message(msg)
        if not replayer.book.has_snapshot:
            continue

        while remaining and remaining[0] == msg.ts_ms:
            t = remaining.pop(0)
            for aid, off in targets.get(t, []):
                ep, _, _ = ep_by_id[aid]
                book_side = wall_book_side(ep.direction)
                capture[aid][t] = snapshot_wall(
                    replayer.book, ts_ms=t, level=ep.level, book_side=book_side
                )

        # dense samples
        for aid, ts_list in list(dense_remaining.items()):
            while ts_list and ts_list[0] <= msg.ts_ms:
                t = ts_list.pop(0)
                if t in capture[aid]:
                    continue
                ep, _, _ = ep_by_id[aid]
                book_side = wall_book_side(ep.direction)
                capture[aid][t] = snapshot_wall(
                    replayer.book, ts_ms=t, level=ep.level, book_side=book_side
                )
            if not ts_list:
                dense_remaining.pop(aid, None)

    # flush leftover targets
    while remaining:
        t = remaining.pop(0)
        if not replayer.book.has_snapshot:
            continue
        for aid, off in targets.get(t, []):
            ep, _, _ = ep_by_id[aid]
            book_side = wall_book_side(ep.direction)
            capture[aid][t] = snapshot_wall(
                replayer.book, ts_ms=t, level=ep.level, book_side=book_side
            )

    seq = replayer.diag.status()
    quality_base["sequence_status"] = seq.value
    if seq == SequenceStatus.INVALID:
        quality_base["data_quality"] = "DATA_WARNING"
    else:
        quality_base["data_quality"] = "DATA_VALID"

    features: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []

    for ep, alabel, ams in anchors:
        book_side = wall_book_side(ep.direction)
        aggressor = aggressor_side_for_direction(ep.direction)
        snaps = capture.get(ep.approach_id, {})
        row: dict[str, Any] = {
            "approach_id": ep.approach_id,
            "symbol": ep.symbol,
            "date": ep.date,
            "timeframe": ep.timeframe,
            "direction": ep.direction,
            "side": ep.side,
            "outcome": ep.outcome,
            "level": ep.level,
            "anchor_type": alabel,
            "anchor_ts_ms": ams,
            "primary_anchor_bps": PRIMARY_ANCHOR_BPS,
            "zone_bps": ZONE_BPS,
            "book_side": book_side,
            "aggressor_side": aggressor,
        }

        # control features from snaps / episode (causal at anchor)
        snap0 = _nearest_snap(snaps, ams)
        dist_at_anchor = None
        if snap0 and snap0.mid is not None and ep.level > 0:
            # direction-normalized distance: positive = still defending (safe side)
            if ep.side == "low":
                dist_at_anchor = (snap0.mid - ep.level) / ep.level * 1e4
            else:
                dist_at_anchor = (ep.level - snap0.mid) / ep.level * 1e4
        row["distance_to_level_bps"] = dist_at_anchor

        # approach speed: change in abs distance from episode start to anchor (bps / min)
        start_ms = ep.episode_start_ms
        dt_min = max(1e-6, (ams - start_ms) / 60_000.0)
        if ep.min_abs_dist_bps is not None and dist_at_anchor is not None:
            # approximate start dist as ENTRY or 50
            start_dist = 50.0
            row["approach_speed_bps_per_min"] = (start_dist - abs(dist_at_anchor)) / dt_min
        else:
            row["approach_speed_bps_per_min"] = None

        # recent return / vol proxies from mid path in [-30s, 0]
        mids = []
        for t, s in sorted(snaps.items()):
            if ams - 30_000 <= t <= ams and s.mid is not None:
                mids.append((t, s.mid))
        if len(mids) >= 2 and mids[0][1] > 0:
            ret = (mids[-1][1] - mids[0][1]) / mids[0][1] * 1e4
            # direction-normalized: positive = moving toward break
            if ep.side == "low":
                row["recent_return_toward_break_bps"] = -ret
            else:
                row["recent_return_toward_break_bps"] = ret
            rets = []
            for i in range(1, len(mids)):
                if mids[i - 1][1] > 0:
                    rets.append(abs((mids[i][1] - mids[i - 1][1]) / mids[i - 1][1] * 1e4))
            row["short_term_vol_bps"] = sum(rets) / len(rets) if rets else None
        else:
            row["recent_return_toward_break_bps"] = None
            row["short_term_vol_bps"] = None

        row["time_spent_near_level_s"] = max(0.0, (ams - start_ms) / 1000.0)
        row["prior_touches_in_episode"] = 1 if ep.first_touch_ts and _parse_ms(ep.first_touch_ts) <= ams else 0

        z0 = float(snap0.zone_qty) if snap0 else None
        w0 = float(snap0.wall_qty) if snap0 else None
        row["wall_qty_0s"] = w0
        row["zone_qty_0s"] = z0

        pull_start_ms = None
        # pull_start: first dense sample where zone <= 80% of initial (20% drop), sustained
        if z0 and z0 > 0:
            for t in sorted(snaps.keys()):
                if t < ams or t > ams + 60_000:
                    continue
                s = snaps[t]
                if s.zone_qty <= z0 * 0.80:
                    pull_start_ms = t
                    break
        row["pull_start_ts_ms"] = pull_start_ms
        row["pull_start_offset_s"] = (
            (pull_start_ms - ams) / 1000.0 if pull_start_ms is not None else None
        )

        for off in PULL_OFFSETS_S:
            if off == 0:
                continue
            t = ams + off * 1000
            sn = _nearest_snap(snaps, t)
            z = float(sn.zone_qty) if sn else None
            w = float(sn.wall_qty) if sn else None
            row[f"zone_qty_{off}s"] = z
            row[f"wall_qty_{off}s"] = w
            if z0 is not None and z is not None:
                abs_red = max(0.0, z0 - z)
                pct_red = abs_red / z0 if z0 > 0 else 0.0
                flow = aggressive_flow_in_window(
                    trades,
                    start_ms=ams,
                    end_ms=t,
                    aggressor_side=aggressor,
                    ref_price=ep.level,
                )
                # gross refill approx: max(0, increases) — use max(0, z - z0 + flow) weak proxy
                passive_excess = max(0.0, abs_red - flow)
                row[f"zone_abs_reduction_{off}s"] = abs_red
                row[f"zone_pct_reduction_{off}s"] = pct_red
                row[f"matched_aggressive_qty_{off}s"] = flow
                row[f"consumption_ratio_{off}s"] = (flow / abs_red) if abs_red > 1e-12 else None
                row[f"passive_removal_excess_{off}s"] = passive_excess
                row[f"passive_removal_excess_pct_{off}s"] = (
                    passive_excess / z0 if z0 > 0 else 0.0
                )
                # direction-normalized pull pressure: higher = more defensive liquidity gone
                row[f"pull_pressure_{off}s"] = row[f"passive_removal_excess_pct_{off}s"]
            else:
                for k in (
                    f"zone_abs_reduction_{off}s",
                    f"zone_pct_reduction_{off}s",
                    f"matched_aggressive_qty_{off}s",
                    f"consumption_ratio_{off}s",
                    f"passive_removal_excess_{off}s",
                    f"passive_removal_excess_pct_{off}s",
                    f"pull_pressure_{off}s",
                ):
                    row[k] = None

        # primary pull feature: 30s window passive excess pct
        row["primary_pull_feature"] = row.get("passive_removal_excess_pct_30s")
        row["primary_pull_feature_name"] = "passive_removal_excess_pct_30s"

        # delete rate / pulled levels proxy from dense path
        deletes = 0
        reductions = 0
        prev_z = None
        for t in sorted(snaps.keys()):
            if t < ams or t > ams + 60_000:
                continue
            z = snaps[t].zone_qty
            if prev_z is not None:
                if prev_z > 0 and z <= 0:
                    deletes += 1
                if z < prev_z * 0.98:
                    reductions += 1
            prev_z = z
        row["delete_events_60s"] = deletes
        row["reduction_events_60s"] = reductions

        if ep.outcome == "LEVEL_BREAK" and ep.first_break_ts:
            bms = _parse_ms(ep.first_break_ts)
            row["seconds_from_anchor_to_break"] = (bms - ams) / 1000.0 if bms else None
            if pull_start_ms is not None and bms is not None:
                row["seconds_from_pull_start_to_break"] = (bms - pull_start_ms) / 1000.0
            else:
                row["seconds_from_pull_start_to_break"] = None
        else:
            row["seconds_from_anchor_to_break"] = None
            row["seconds_from_pull_start_to_break"] = None

        features.append(row)

        # compact timeline rows for examples
        for off in PULL_OFFSETS_S:
            t = ams + off * 1000
            sn = _nearest_snap(snaps, t)
            timelines.append(
                {
                    "approach_id": ep.approach_id,
                    "outcome": ep.outcome,
                    "offset_s": off,
                    "ts_ms": t,
                    "zone_qty": sn.zone_qty if sn else None,
                    "wall_qty": sn.wall_qty if sn else None,
                    "mid": sn.mid if sn else None,
                    "distance_to_level_bps": sn.distance_to_level_bps if sn else None,
                }
            )

    qualities = [{**quality_base, "n_approaches": len(anchors)}]
    return features, timelines, qualities
