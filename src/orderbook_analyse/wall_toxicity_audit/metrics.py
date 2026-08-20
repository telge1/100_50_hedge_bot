"""Trade alignment and pulling / migration metrics."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from orderbook_analyse.wall_toxicity_audit.bucket import ticks_between
from orderbook_analyse.wall_toxicity_audit.level_state import iter_complete_changes
from orderbook_analyse.wall_toxicity_audit.types import (
    LevelQtyEvent,
    MigrationEvent,
    MigrationMetrics,
    PullMetrics,
    TradeAlignmentRow,
    WallToxicityParams,
)


@dataclass(frozen=True)
class TradeTick:
    ts: datetime
    side: str
    price: float
    qty: float
    notional: float


def aggressive_side_for_wall(wall_side: str) -> str:
    """Trades that hit an ask wall are aggressive buys; bid wall → sells."""
    if str(wall_side).lower() in {"ask", "sell"}:
        return "Buy"
    return "Sell"


def align_trades(
    trades: Sequence[TradeTick],
    *,
    wall_side: str,
    band_low: float,
    band_high: float,
) -> list[TradeAlignmentRow]:
    want = aggressive_side_for_wall(wall_side).lower()
    out: list[TradeAlignmentRow] = []
    for t in trades:
        side = str(t.side)
        in_bucket = band_low - 1e-15 <= t.price <= band_high + 1e-15
        aggressive = side.lower() == want.lower() or (
            want == "buy" and side.lower() in {"buy", "b"}
        ) or (want == "sell" and side.lower() in {"sell", "s"})
        if want == "Buy":
            aggressive = side.lower() in {"buy", "b"}
        else:
            aggressive = side.lower() in {"sell", "s"}
        out.append(
            TradeAlignmentRow(
                ts=t.ts,
                symbol="",
                side=side,
                price=float(t.price),
                qty=float(t.qty),
                notional=float(t.notional),
                in_bucket=in_bucket,
                aggressive_vs_wall=aggressive,
            )
        )
    return out


def _trade_qty_covering_removal(
    *,
    remove_ts: datetime,
    price: float,
    removed_qty: float,
    trades: Sequence[TradeTick],
    wall_side: str,
    window_ms: float,
) -> float:
    """Sum aggressive trades near the level that could explain a removal."""
    if removed_qty <= 0:
        return 0.0
    want = aggressive_side_for_wall(wall_side)
    w = timedelta(milliseconds=window_ms)
    covered = 0.0
    for t in trades:
        if t.ts < remove_ts - w or t.ts > remove_ts + w:
            continue
        side_ok = (
            (want == "Buy" and t.side.lower() in {"buy", "b"})
            or (want == "Sell" and t.side.lower() in {"sell", "s"})
        )
        if not side_ok:
            continue
        # Match same price level (tight) — do not claim fill without price match.
        if abs(t.price - price) > 1e-12:
            continue
        covered += float(t.qty)
        if covered >= removed_qty:
            return removed_qty
    return min(covered, removed_qty)


def compute_pull_metrics(
    events: Sequence[LevelQtyEvent],
    trades: Sequence[TradeTick],
    *,
    params: WallToxicityParams,
    wall_side: str,
    bucket_touched: bool,
    first_touch_ts: datetime | None,
) -> PullMetrics:
    m = PullMetrics()
    primary_events = [e for e in iter_complete_changes(events) if e.in_primary_bucket]
    for ev in primary_events:
        ch = float(ev.qty_change or 0.0)
        if ch > 0:
            m.gross_added_qty += ch
        elif ch < 0:
            removed = -ch
            m.gross_removed_qty += removed
            explained = _trade_qty_covering_removal(
                remove_ts=ev.ts,
                price=ev.price,
                removed_qty=removed,
                trades=trades,
                wall_side=wall_side,
                window_ms=params.trade_match_window_ms,
            )
            unexplained = max(0.0, removed - explained)
            m.removed_without_trade_qty += unexplained
            prev = float(ev.previous_qty or 0.0)
            pct = (removed / prev * 100.0) if prev > 0 else 0.0
            is_large = removed >= params.large_pull_min_qty or pct >= params.large_pull_min_pct
            if is_large:
                m.large_pull_count += 1
                if removed > m.largest_single_pull_qty:
                    m.largest_single_pull_qty = removed
                    m.largest_single_pull_pct = pct
            if first_touch_ts is None or ev.ts < first_touch_ts:
                m.pull_events_before_touch += 1
            else:
                m.pull_events_near_touch += 1
            if not bucket_touched:
                # all pulls are before touch if never touched
                pass

    m.net_bucket_change = m.gross_added_qty - m.gross_removed_qty
    if m.gross_removed_qty > 0:
        m.removed_without_trade_ratio = m.removed_without_trade_qty / m.gross_removed_qty
    else:
        m.removed_without_trade_ratio = None

    want = aggressive_side_for_wall(wall_side)
    for t in trades:
        side_ok = (
            (want == "Buy" and t.side.lower() in {"buy", "b"})
            or (want == "Sell" and t.side.lower() in {"sell", "s"})
        )
        # caller filters trades to band; count all provided
        if side_ok:
            m.trade_qty_in_bucket += float(t.qty)
            m.trade_count_in_bucket += 1
    return m


def detect_migrations(
    events: Sequence[LevelQtyEvent],
    trades: Sequence[TradeTick],
    *,
    params: WallToxicityParams,
    wall_side: str,
    tick_size: float,
    mid_at: dict[datetime, float] | None = None,
    mid_series: Sequence[tuple[datetime, float]] | None = None,
) -> tuple[list[MigrationEvent], MigrationMetrics]:
    """Quantity/time matching only — does not claim order identity."""
    removes: list[LevelQtyEvent] = []
    adds: list[LevelQtyEvent] = []
    for ev in iter_complete_changes(events):
        ch = float(ev.qty_change or 0.0)
        if ch < 0 and abs(ch) >= params.migration_min_qty * 0.25:
            removes.append(ev)
        elif ch > 0 and ch >= params.migration_min_qty * 0.25:
            adds.append(ev)

    window = timedelta(milliseconds=params.migration_window_ms)
    tol = params.migration_qty_tolerance_pct / 100.0
    used_adds: set[int] = set()
    migrations: list[MigrationEvent] = []

    def _mid_near(ts: datetime) -> float | None:
        if mid_at and ts in mid_at:
            return mid_at[ts]
        if not mid_series:
            return None
        best = None
        best_dt = None
        for t, m in mid_series:
            dt = abs((t - ts).total_seconds())
            if best_dt is None or dt < best_dt:
                best_dt = dt
                best = m
            if t > ts and best_dt is not None and dt > 2:
                break
        return best

    for rem in removes:
        removed = -float(rem.qty_change or 0.0)
        explained = _trade_qty_covering_removal(
            remove_ts=rem.ts,
            price=rem.price,
            removed_qty=removed,
            trades=trades,
            wall_side=wall_side,
            window_ms=params.trade_match_window_ms,
        )
        unexplained = removed - explained
        if unexplained < params.migration_min_qty * 0.5:
            continue
        best_i = None
        best_score = None
        for i, add in enumerate(adds):
            if i in used_adds:
                continue
            if add.price == rem.price:
                continue
            if add.ts < rem.ts or add.ts > rem.ts + window:
                continue
            added = float(add.qty_change or 0.0)
            # similar magnitude
            rel = abs(added - unexplained) / max(unexplained, 1e-9)
            if rel > tol and abs(added - removed) / max(removed, 1e-9) > tol:
                continue
            delay = (add.ts - rem.ts).total_seconds() * 1000.0
            score = (rel, delay)
            if best_score is None or score < best_score:
                best_score = score
                best_i = i
        if best_i is None:
            continue
        add = adds[best_i]
        used_adds.add(best_i)
        added = float(add.qty_change or 0.0)
        matched = min(unexplained, added)
        mid = _mid_near(rem.ts)
        toward: bool | None = None
        if mid is not None:
            # ask wall: toward market = price decreases toward mid
            if str(wall_side).lower() in {"ask", "sell"}:
                toward = add.price < rem.price
            else:
                toward = add.price > rem.price
        migrations.append(
            MigrationEvent(
                ts_remove=rem.ts,
                ts_add=add.ts,
                delay_ms=(add.ts - rem.ts).total_seconds() * 1000.0,
                side=wall_side,
                price_from=rem.price,
                price_to=add.price,
                distance_ticks=ticks_between(rem.price, add.price, tick_size),
                removed_qty=removed,
                added_qty=added,
                matched_qty=matched,
                toward_market=toward,
                mid_at_event=mid,
                trade_explained_qty=explained,
            )
        )

    metrics = MigrationMetrics(migration_event_count=len(migrations))
    if migrations:
        metrics.migrated_qty = sum(m.matched_qty for m in migrations)
        delays = [m.delay_ms for m in migrations]
        dists = [m.distance_ticks for m in migrations]
        metrics.median_migration_delay_ms = float(statistics.median(delays))
        metrics.median_migration_distance_ticks = float(statistics.median(dists))
        for m in migrations:
            if m.toward_market is True:
                metrics.moved_toward_market_qty += m.matched_qty
            elif m.toward_market is False:
                metrics.moved_away_from_market_qty += m.matched_qty
        # oscillation: A→B and later B→A
        pairs = {(round(m.price_from, 10), round(m.price_to, 10)) for m in migrations}
        osc = 0
        for a, b in list(pairs):
            if (b, a) in pairs:
                osc += 1
        metrics.oscillating_liquidity_count = osc // 2
    return migrations, metrics
