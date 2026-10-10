"""
Chronological short baseline walk using historical V2 15m pools.

Research fill assumption: entry at 15m bar close (same as floor_guard_signal_list).
No fees or slippage (inherits analyze_xrp_floor_guard_pnl behavior).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .errors import SnapshotMissingError
from .guard_v2 import V2H4FloorGuard
from .provider import HistoricalLmV2PoolStore
from .strategy_paths import ensure_strategy_paths
from .variants import BaselineVariant


@dataclass
class WalkState:
    seen_pool_ids: set[str] = field(default_factory=set)
    floor_blocked_events: int = 0


@dataclass
class WalkFunnel:
    bars_replayed: int = 0
    decisions_in_report: int = 0
    entry_setup_candidates: int = 0
    down_4h_blocked: int = 0
    floor_guard_blocked: int = 0
    signals_emitted: int = 0
    duplicate_pool_skipped: int = 0


@dataclass
class SignalRow:
    raw: dict[str, Any]
    variant: str
    floor_guard_blocked: bool


def four_h_closes_from_market(market: dict) -> list[datetime]:
    return [bar["close_time"] for bar in market["4h"]["bars"]]


def run_baseline_walk(
    symbol: str,
    bars15: list[dict],
    market: dict,
    store: HistoricalLmV2PoolStore,
    variant: BaselineVariant,
    *,
    pane_from: datetime,
    pane_to: datetime,
    replay_from: datetime,
    report_from: datetime,
    report_to: datetime,
    state: WalkState | None = None,
    funnel: WalkFunnel | None = None,
) -> tuple[list[SignalRow], WalkState, WalkFunnel]:
    """
    Replay from ``replay_from`` (guard + seen state), evaluate signals only in
    ``[report_from, report_to]``. Guard ``advance`` runs for every bar in
    ``bars15`` with ``close_time >= replay_from`` (bars before that are skipped).
    """
    ensure_strategy_paths()
    from find_short_entry_15m_v1 import (
        down_4h,
        entry_setup,
        last_closed_index,
        row_for,
    )

    st = state or WalkState()
    fn = funnel or WalkFunnel()
    by_open = {bar["open_time"]: bar for bar in bars15}
    guard: V2H4FloorGuard | None = None
    if variant.floor_guard_enabled:
        guard = V2H4FloorGuard(
            symbol,
            pane_from,
            pane_to,
            store,
            pool_detail_4h=variant.pool_detail_4h,
            four_h_bar_closes=four_h_closes_from_market(market),
        )

    rows: list[SignalRow] = []
    for bar in bars15:
        moment = bar["close_time"]
        if moment < replay_from:
            continue
        fn.bars_replayed += 1

        blocked = False
        if guard is not None:
            blocked, _reason = guard.advance(bar, moment)

        in_report = report_from <= moment <= report_to
        if not in_report:
            continue

        fn.decisions_in_report += 1
        if blocked:
            fn.floor_guard_blocked += 1
            st.floor_blocked_events += 1

        try:
            pools = store.load_legacy_pools("15m", variant.pool_detail_15m, moment)
        except SnapshotMissingError:
            raise

        setup = entry_setup(pools, bar, moment)
        if setup is None:
            continue
        fn.entry_setup_candidates += 1
        pool, upper, lower, gap = setup
        h4_index = last_closed_index(market["4h"]["bars"], moment)
        if not down_4h(market["4h"]["bars"], h4_index):
            fn.down_4h_blocked += 1
            continue
        h4 = market["4h"]["bars"][h4_index]
        source_t = pool.get("source")
        birth = by_open.get(source_t, bar) if source_t is not None else bar
        row = row_for(symbol, pool, birth, bar, h4, None, None, upper, lower, gap)

        if pool["pool_id"] in st.seen_pool_ids:
            fn.duplicate_pool_skipped += 1
            continue

        if variant.floor_guard_enabled and blocked:
            continue

        st.seen_pool_ids.add(pool["pool_id"])
        fn.signals_emitted += 1
        variant_label = "with_guard" if variant.floor_guard_enabled else "without_guard"
        rows.append(
            SignalRow(
                raw=row,
                variant=variant_label,
                floor_guard_blocked=False,
            )
        )

    return rows, st, fn


def enrich_trades(bars15: list[dict], signals: list[SignalRow], symbol: str) -> list[dict]:
    """Unchanged ``analyze_xrp_floor_guard_pnl_v1.enrich_signals``."""
    ensure_strategy_paths()
    from analyze_xrp_floor_guard_pnl_v1 import enrich_signals

    raw_rows = [s.raw for s in signals]
    return enrich_signals(bars15, raw_rows)
