"""Scan one symbol for short pool-bounce candidates (dry-run, no orders)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bot.forward_test.config import (
    BAR_LOOKBACK_HOURS,
    FLOW_DELTA_OK,
    FLOW_OB_OK,
    MAX_ENTRY_LAG_MINUTES,
    MAX_RANKS,
    MIN_TP_ROOM_PCT,
    POOL_LOOKBACK_HOURS,
    POOL_LOOKFORWARD_HOURS,
    SL_ABOVE_POOL_TOP_PCT,
    WATCH_BEFORE_PCT,
)
from bot.forward_test.models import DrySignal, ScanEvent
from bot.forward_test.paths import ensure_import_paths


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bar_ts(bar: Any) -> datetime:
    ts = bar.ts
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _ob_ratio(symbol: str, when: datetime) -> float | None:
    """Live OB ratio from collector OB1000 archive (not full_ob_v1 backtest path)."""
    from bot.forward_test.ob_live import sample_ob1000_ratio

    return sample_ob1000_ratio(symbol, when)


def _delta_10m(symbol: str, end: datetime) -> float | None:
    from ob_microstructure_breakout_bot.data.trades import load_trade_window

    try:
        win = load_trade_window(symbol, end - timedelta(minutes=10), end)
        return float(win.delta_notional)
    except Exception:
        return None


def scan_symbol_short(symbol: str, *, now: datetime | None = None) -> tuple[list[ScanEvent], list[DrySignal]]:
    """One dry-run pass for a symbol using frozen short bounce rules."""
    ensure_import_paths()

    from ob_microstructure_breakout_bot.data.bars import load_5m_bars
    from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import group_pools_into_clusters
    from ob_microstructure_breakout_bot.exit_backtest.pool_bounce import (
        load_active_upper_pools_5m,
        rank_clusters_by_mass,
    )
    from ob_microstructure_breakout_bot.exit_backtest.pool_bounce_backtest import (
        find_short_reversal_entry,
    )
    from bot.forward_test.tp_select import select_tp_with_1m_fallback

    as_of = now or _utc_now()
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    as_of = as_of.astimezone(timezone.utc)
    ts_iso = _iso(as_of) or ""
    events: list[ScanEvent] = []
    signals: list[DrySignal] = []

    try:
        bars = load_5m_bars(
            symbol,
            as_of - timedelta(hours=BAR_LOOKBACK_HOURS),
            as_of,
        )
    except Exception as exc:
        events.append(
            ScanEvent(
                ts=ts_iso,
                symbol=symbol,
                event="error",
                reason="bars_load_failed",
                detail={"error": str(exc)},
            )
        )
        return events, signals

    if not bars:
        events.append(
            ScanEvent(
                ts=ts_iso,
                symbol=symbol,
                event="error",
                reason="no_bars",
            )
        )
        return events, signals

    last = bars[-1]
    last_price = float(last.close)
    last_high = float(last.high)

    try:
        upper = load_active_upper_pools_5m(
            symbol,
            as_of,
            last_price,
            lookback_hours=POOL_LOOKBACK_HOURS,
            lookforward_hours=POOL_LOOKFORWARD_HOURS,
        )
    except Exception as exc:
        events.append(
            ScanEvent(
                ts=ts_iso,
                symbol=symbol,
                event="error",
                reason="pool_load_failed",
                last_price=last_price,
                detail={"error": str(exc)},
            )
        )
        return events, signals

    if not upper:
        events.append(
            ScanEvent(
                ts=ts_iso,
                symbol=symbol,
                event="idle",
                reason="no_active_upper_pools",
                last_price=last_price,
            )
        )
        return events, signals

    clusters = group_pools_into_clusters(upper, entry=last_price)
    ranked = rank_clusters_by_mass(clusters)[:MAX_RANKS]
    if not ranked:
        events.append(
            ScanEvent(
                ts=ts_iso,
                symbol=symbol,
                event="idle",
                reason="no_rank12_clusters",
                last_price=last_price,
            )
        )
        return events, signals

    any_active = False
    for rank, cluster in enumerate(ranked, start=1):
        pool_bottom = float(cluster.bottom)
        pool_top = float(cluster.top)
        watch_trigger = pool_bottom * (1.0 - WATCH_BEFORE_PCT / 100.0)
        dist_pct = (pool_bottom - last_price) / pool_bottom * 100.0 if pool_bottom > 0 else None

        # Already above pool top without watching → pierce / ignore for approach.
        if last_price > pool_top:
            continue

        in_watch = last_high >= watch_trigger
        touched = last_high >= pool_bottom
        if not in_watch and not touched:
            continue

        any_active = True
        sample_ts = last.ts if last.ts.tzinfo else last.ts.replace(tzinfo=timezone.utc)
        ob = _ob_ratio(symbol, sample_ts)
        delta = _delta_10m(symbol, sample_ts)
        flow_ok = (
            ob is not None
            and delta is not None
            and ob >= FLOW_OB_OK
            and delta >= FLOW_DELTA_OK
        )

        if touched:
            # Live parity: only the *current* approach counts.
            # Using the first touch in the full lookback re-binds hours-old
            # touch_ts / flow / entry and floods stale_entry / frozen flow skips.
            touch_horizon = as_of - timedelta(minutes=float(MAX_ENTRY_LAG_MINUTES) + 5.0)
            recent_touch_idxs = [
                i
                for i, b in enumerate(bars)
                if float(b.high) >= pool_bottom and _bar_ts(b) >= touch_horizon
            ]
            if not recent_touch_idxs:
                events.append(
                    ScanEvent(
                        ts=ts_iso,
                        symbol=symbol,
                        event="skip",
                        rank=rank,
                        cluster_id=str(cluster.cluster_id),
                        pool_bottom=pool_bottom,
                        pool_top=pool_top,
                        last_price=last_price,
                        reason="touch_outside_live_horizon",
                        detail={
                            "touch_horizon": _iso(touch_horizon),
                            "max_entry_lag_minutes": MAX_ENTRY_LAG_MINUTES,
                        },
                    )
                )
                continue

            # First touch of the *recent* episode (not of the whole 6h window).
            touch_idx = recent_touch_idxs[0]
            touch_bar = bars[touch_idx]
            touch_ts = _bar_ts(touch_bar)
            ob_touch = _ob_ratio(symbol, touch_ts)
            delta_touch = _delta_10m(symbol, touch_ts)
            flow_touch = (
                ob_touch is not None
                and delta_touch is not None
                and ob_touch >= FLOW_OB_OK
                and delta_touch >= FLOW_DELTA_OK
            )

            events.append(
                ScanEvent(
                    ts=ts_iso,
                    symbol=symbol,
                    event="touch",
                    rank=rank,
                    cluster_id=str(cluster.cluster_id),
                    pool_bottom=pool_bottom,
                    pool_top=pool_top,
                    last_price=last_price,
                    dist_to_pool_pct=dist_pct,
                    ob_ratio=ob_touch,
                    delta_10m=delta_touch,
                    flow_confirmed=flow_touch,
                    reason="pool_touched",
                    detail={"touch_ts": _iso(touch_ts)},
                )
            )

            if not flow_touch:
                events.append(
                    ScanEvent(
                        ts=ts_iso,
                        symbol=symbol,
                        event="skip",
                        rank=rank,
                        cluster_id=str(cluster.cluster_id),
                        pool_bottom=pool_bottom,
                        pool_top=pool_top,
                        last_price=last_price,
                        ob_ratio=ob_touch,
                        delta_10m=delta_touch,
                        flow_confirmed=False,
                        reason="flow_not_confirmed",
                    )
                )
                continue

            rev = find_short_reversal_entry(
                bars,
                touch_idx=touch_idx,
                pool_bottom=pool_bottom,
                pool_top=pool_top,
                sl_above_pct=SL_ABOVE_POOL_TOP_PCT,
            )
            if not rev.get("ok"):
                events.append(
                    ScanEvent(
                        ts=ts_iso,
                        symbol=symbol,
                        event="skip",
                        rank=rank,
                        cluster_id=str(cluster.cluster_id),
                        pool_bottom=pool_bottom,
                        pool_top=pool_top,
                        last_price=last_price,
                        ob_ratio=ob_touch,
                        delta_10m=delta_touch,
                        flow_confirmed=True,
                        reason=str(rev.get("reason") or "no_entry"),
                        detail={"stop_price": rev.get("stop_price")},
                    )
                )
                continue

            entry_ts = rev["entry_ts"]
            if isinstance(entry_ts, datetime):
                entry_ts_utc = (
                    entry_ts.replace(tzinfo=timezone.utc)
                    if entry_ts.tzinfo is None
                    else entry_ts.astimezone(timezone.utc)
                )
            else:
                entry_ts_utc = None
            # No lookahead / late backfill: only act on fresh reversal candles.
            if entry_ts_utc is not None:
                lag_min = (as_of - entry_ts_utc).total_seconds() / 60.0
                if lag_min > float(MAX_ENTRY_LAG_MINUTES):
                    events.append(
                        ScanEvent(
                            ts=ts_iso,
                            symbol=symbol,
                            event="skip",
                            rank=rank,
                            cluster_id=str(cluster.cluster_id),
                            pool_bottom=pool_bottom,
                            pool_top=pool_top,
                            last_price=last_price,
                            ob_ratio=ob_touch,
                            delta_10m=delta_touch,
                            flow_confirmed=True,
                            reason="stale_entry",
                            detail={
                                "short_entry_ts": _iso(entry_ts_utc),
                                "lag_minutes": round(lag_min, 2),
                                "max_entry_lag_minutes": MAX_ENTRY_LAG_MINUTES,
                            },
                        )
                    )
                    continue

            entry_price = float(rev["entry_price"])
            stop_price = float(rev["stop_price"])
            try:
                tp_pick = select_tp_with_1m_fallback(
                    symbol=symbol,
                    entry_ts=entry_ts,
                    entry_price=entry_price,
                    min_room_pct=MIN_TP_ROOM_PCT,
                )
            except Exception as exc:
                events.append(
                    ScanEvent(
                        ts=ts_iso,
                        symbol=symbol,
                        event="error",
                        rank=rank,
                        cluster_id=str(cluster.cluster_id),
                        reason="tp_select_failed",
                        detail={"error": str(exc)},
                    )
                )
                continue

            if not tp_pick.get("ok"):
                events.append(
                    ScanEvent(
                        ts=ts_iso,
                        symbol=symbol,
                        event="skip",
                        rank=rank,
                        cluster_id=str(cluster.cluster_id),
                        pool_bottom=pool_bottom,
                        pool_top=pool_top,
                        last_price=last_price,
                        flow_confirmed=True,
                        reason="no_tp_room",
                        detail={
                            "tp_reason": tp_pick.get("reason"),
                            "five_m_tp_price": tp_pick.get("five_m_tp_price"),
                            "five_m_tp_room_pct": tp_pick.get("five_m_tp_room_pct"),
                        },
                    )
                )
                continue

            tp_price = float(tp_pick["tp_price"])
            tp_room = float(tp_pick["tp_room_pct"])
            tp_timeframe = str(tp_pick.get("tp_timeframe") or "5m")
            tp_source = str(tp_pick.get("tp_source") or "5m_lower_pool")

            events.append(
                ScanEvent(
                    ts=ts_iso,
                    symbol=symbol,
                    event="entry_candidate",
                    rank=rank,
                    cluster_id=str(cluster.cluster_id),
                    pool_bottom=pool_bottom,
                    pool_top=pool_top,
                    last_price=last_price,
                    dist_to_pool_pct=dist_pct,
                    ob_ratio=ob_touch,
                    delta_10m=delta_touch,
                    flow_confirmed=True,
                    reason="dry_signal_ready",
                    detail={
                        "entry_price": entry_price,
                        "stop_price": stop_price,
                        "tp_price": tp_price,
                        "tp_room_pct": tp_room,
                        "tp_timeframe": tp_timeframe,
                        "tp_source": tp_source,
                        "tp_reason": tp_pick.get("reason"),
                        "five_m_tp_price": tp_pick.get("five_m_tp_price"),
                        "five_m_tp_room_pct": tp_pick.get("five_m_tp_room_pct"),
                        "short_entry_ts": _iso(entry_ts),
                    },
                )
            )
            signals.append(
                DrySignal(
                    ts=ts_iso,
                    symbol=symbol,
                    side="short",
                    rank=rank,
                    cluster_id=str(cluster.cluster_id),
                    entry_price=entry_price,
                    stop_price=stop_price,
                    tp_price=tp_price,
                    pool_bottom=pool_bottom,
                    pool_top=pool_top,
                    touch_ts=_iso(touch_ts),
                    short_entry_ts=_iso(entry_ts),
                    ob_ratio_at_touch=ob_touch,
                    delta_at_touch=delta_touch,
                    flow_confirmed=True,
                    tp_room_pct=tp_room,
                    tp_timeframe=tp_timeframe,
                    tp_source=tp_source,
                )
            )
            continue

        # Approach / watch zone only.
        events.append(
            ScanEvent(
                ts=ts_iso,
                symbol=symbol,
                event="watch",
                rank=rank,
                cluster_id=str(cluster.cluster_id),
                pool_bottom=pool_bottom,
                pool_top=pool_top,
                last_price=last_price,
                dist_to_pool_pct=dist_pct,
                ob_ratio=ob,
                delta_10m=delta,
                flow_confirmed=flow_ok,
                reason="within_watch_distance",
                detail={"watch_trigger": watch_trigger},
            )
        )

    if not any_active:
        events.append(
            ScanEvent(
                ts=ts_iso,
                symbol=symbol,
                event="idle",
                reason="no_rank12_in_watch_or_touch",
                last_price=last_price,
                detail={"n_rank12": len(ranked)},
            )
        )

    return events, signals
