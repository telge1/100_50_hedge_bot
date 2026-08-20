"""Orchestrate wall toxicity audit analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from orderbook_analyse.wall_toxicity_audit.bucket import resolve_bucket
from orderbook_analyse.wall_toxicity_audit.classify import classify_wall
from orderbook_analyse.wall_toxicity_audit.data_access import (
    default_wall_sequences_csv,
    ensure_utc,
    load_best_quotes,
    load_level_updates,
    load_ticker_mids,
    load_trades,
    load_wall_sequence_from_csv,
    open_readonly_db,
    wall_sequence_from_row,
)
from orderbook_analyse.wall_toxicity_audit.export import write_audit_outputs
from orderbook_analyse.wall_toxicity_audit.level_state import LevelStateTracker
from orderbook_analyse.wall_toxicity_audit.metrics import (
    TradeTick,
    align_trades,
    compute_pull_metrics,
    detect_migrations,
)
from orderbook_analyse.wall_toxicity_audit.types import (
    AUDIT_VERSION,
    LevelQtyEvent,
    MarketInteraction,
    MigrationEvent,
    TradeAlignmentRow,
    WallSequenceRef,
    WallToxicityParams,
    WallToxicityResult,
)

logger = logging.getLogger(__name__)


@dataclass
class AuditBundle:
    sequence: WallSequenceRef
    bucket: dict[str, float]
    level_events: list[LevelQtyEvent]
    migrations: list[MigrationEvent]
    trade_rows: list[TradeAlignmentRow]
    trades: list[TradeTick]
    result: WallToxicityResult
    params: WallToxicityParams


def _distance_bps(wall_price: float, *, side: str, best_bid: float | None, best_ask: float | None) -> float | None:
    if str(side).lower() in {"ask", "sell"}:
        ref = best_ask if best_ask is not None else best_bid
    else:
        ref = best_bid if best_bid is not None else best_ask
    if ref is None or ref <= 0 or wall_price <= 0:
        return None
    return abs(wall_price - ref) / ref * 10_000.0


def _market_interaction(
    *,
    sequence: WallSequenceRef,
    quotes: Sequence[tuple[datetime, float | None, float | None]],
    mids: Sequence[tuple[datetime, float]],
    trades_in_bucket: bool,
    pull_before_touch: bool,
    params: WallToxicityParams,
    wall_center: float,
) -> MarketInteraction:
    dists: list[float] = []
    touched = bool(sequence.touched or sequence.was_tested)
    first_touch: datetime | None = None
    for ts, bb, ba in quotes:
        d = _distance_bps(wall_center, side=sequence.side, best_bid=bb, best_ask=ba)
        if d is not None:
            dists.append(d)
            if d <= params.touch_bps:
                touched = True
                if first_touch is None:
                    first_touch = ts
    min_d = min(dists) if dists else sequence.min_distance_bps
    max_d = max(dists) if dists else sequence.max_distance_bps
    if min_d is None and sequence.min_distance_bps is not None:
        min_d = sequence.min_distance_bps
    remained_remote = bool(
        min_d is not None and min_d >= params.remote_min_bps and not touched
    )
    reaction = None
    if mids and len(mids) >= 2:
        # crude: mid change over last third vs first third of window
        n = len(mids)
        a = sum(m for _, m in mids[: max(1, n // 3)]) / max(1, n // 3)
        b = sum(m for _, m in mids[-(max(1, n // 3)) :]) / max(1, n // 3)
        if a > 0:
            reaction = (b - a) / a * 10_000.0
    return MarketInteraction(
        min_distance_bps=min_d,
        max_distance_bps=max_d,
        bucket_touched=touched,
        trades_in_bucket=trades_in_bucket,
        removed_before_touch=bool(pull_before_touch and not touched)
        or bool(sequence.disappeared_before_test),
        remained_remote=remained_remote,
        price_reaction_after_pull_bps=reaction,
    )


def run_audit_on_sequence(
    sequence: WallSequenceRef,
    *,
    params: WallToxicityParams,
    level_rows: Sequence[dict[str, Any]],
    trades: Sequence[TradeTick],
    quotes: Sequence[tuple[datetime, float | None, float | None]],
    mids: Sequence[tuple[datetime, float]],
) -> AuditBundle:
    bucket = resolve_bucket(
        wall_price=sequence.first_price,
        side=sequence.side,
        resolution=sequence.resolution,
        neighbor_buckets=params.neighbor_buckets,
        tick_size=params.tick_size,
    )
    tracker = LevelStateTracker(
        symbol=sequence.symbol,
        side=sequence.side,
        band_low=bucket["band_low"],
        band_high=bucket["band_high"],
        analysis_low=bucket["analysis_low"],
        analysis_high=bucket["analysis_high"],
    )
    for row in level_rows:
        tracker.apply_level(
            ts=ensure_utc(row["exchange_ts"]),
            price=float(row["price"]),
            new_qty=float(row["quantity"]),
            message_type=str(row["message_type"]),
            update_id=int(row["update_id"]),
            cross_sequence=int(row["cross_sequence"]),
        )

    # Trades restricted to analysis band for bucket stats
    band_trades = [
        t
        for t in trades
        if bucket["analysis_low"] - 1e-15 <= t.price <= bucket["analysis_high"] + 1e-15
    ]
    primary_trades = [
        t
        for t in band_trades
        if bucket["band_low"] - 1e-15 <= t.price <= bucket["band_high"] + 1e-15
    ]
    trade_rows = align_trades(
        primary_trades,
        wall_side=sequence.side,
        band_low=bucket["band_low"],
        band_high=bucket["band_high"],
    )
    for tr in trade_rows:
        tr.symbol = sequence.symbol

    market_pre = _market_interaction(
        sequence=sequence,
        quotes=quotes,
        mids=mids,
        trades_in_bucket=len(primary_trades) > 0,
        pull_before_touch=True,
        params=params,
        wall_center=bucket["primary_bucket_price"],
    )
    pull = compute_pull_metrics(
        tracker.events,
        primary_trades,
        params=params,
        wall_side=sequence.side,
        bucket_touched=market_pre.bucket_touched,
        first_touch_ts=None,
    )
    migrations, mig_metrics = detect_migrations(
        tracker.events,
        primary_trades,
        params=params,
        wall_side=sequence.side,
        tick_size=bucket["tick_size"],
        mid_series=mids,
    )
    if pull.gross_removed_qty > 0:
        mig_metrics.migration_ratio = mig_metrics.migrated_qty / pull.gross_removed_qty

    market = _market_interaction(
        sequence=sequence,
        quotes=quotes,
        mids=mids,
        trades_in_bucket=pull.trade_count_in_bucket > 0,
        pull_before_touch=pull.pull_events_before_touch > 0,
        params=params,
        wall_center=bucket["primary_bucket_price"],
    )
    # Prefer CSV distance if ticker sparse
    if market.min_distance_bps is None and sequence.min_distance_bps is not None:
        market.min_distance_bps = sequence.min_distance_bps
        market.remained_remote = sequence.min_distance_bps >= params.remote_min_bps

    incomplete = sum(1 for e in tracker.events if e.incomplete_initial)
    incomplete_ratio = incomplete / max(len(tracker.events), 1)
    result = classify_wall(
        pull=pull,
        migration=mig_metrics,
        market=market,
        params=params,
        incomplete_ratio=incomplete_ratio,
        sample_event_count=len(tracker.events),
    )
    return AuditBundle(
        sequence=sequence,
        bucket=bucket,
        level_events=tracker.events,
        migrations=migrations,
        trade_rows=trade_rows,
        trades=list(primary_trades),
        result=result,
        params=params,
    )


def run_wall_toxicity_audit(
    *,
    symbol: str,
    sequence_id: str,
    output_dir: Path | None = None,
    params: WallToxicityParams | None = None,
    wall_sequences_csv: Path | None = None,
    level_rows: Sequence[dict[str, Any]] | None = None,
    trades: Sequence[TradeTick] | None = None,
    quotes: Sequence[tuple[datetime, float | None, float | None]] | None = None,
    mids: Sequence[tuple[datetime, float]] | None = None,
    write_outputs: bool = True,
    db: Any | None = None,
    sequence: WallSequenceRef | None = None,
) -> AuditBundle:
    """Run audit from ClickHouse or injected fixtures (tests).

    When ``db`` is provided, the caller owns the connection lifecycle.
    """
    params = params or WallToxicityParams()
    csv_path = wall_sequences_csv or default_wall_sequences_csv(symbol)
    if sequence is None:
        if csv_path is None:
            raise FileNotFoundError(
                f"No wall_sequences.csv found for {symbol}; pass --wall-sequences-csv"
            )
        sequence = load_wall_sequence_from_csv(csv_path, sequence_id=sequence_id)
    if sequence.symbol != symbol:
        raise ValueError(
            f"sequence symbol {sequence.symbol} != requested {symbol}"
        )

    bucket = resolve_bucket(
        wall_price=sequence.first_price,
        side=sequence.side,
        resolution=sequence.resolution,
        neighbor_buckets=params.neighbor_buckets,
        tick_size=params.tick_size,
    )
    start = sequence.first_seen_ts - timedelta(seconds=params.warmup_seconds)
    end = (sequence.closed_ts or sequence.last_seen_ts) + timedelta(
        seconds=params.post_window_seconds
    )

    owns_db = False
    try:
        if level_rows is None or trades is None or quotes is None or mids is None:
            if db is None:
                db = open_readonly_db()
                owns_db = True
            if level_rows is None:
                level_rows = load_level_updates(
                    db,
                    symbol=symbol,
                    side=sequence.side,
                    price_low=bucket["analysis_low"],
                    price_high=bucket["analysis_high"],
                    start=start,
                    end=end,
                )
            if trades is None:
                trades = load_trades(
                    db,
                    symbol=symbol,
                    start=start,
                    end=end,
                    price_low=bucket["analysis_low"],
                    price_high=bucket["analysis_high"],
                )
            if quotes is None:
                quotes = load_best_quotes(db, symbol=symbol, start=start, end=end)
            if mids is None:
                mids = load_ticker_mids(db, symbol=symbol, start=start, end=end)
    finally:
        if owns_db and db is not None:
            db.close()

    bundle = run_audit_on_sequence(
        sequence,
        params=params,
        level_rows=level_rows or [],
        trades=trades or [],
        quotes=quotes or [],
        mids=mids or [],
    )
    if write_outputs:
        if output_dir is None:
            raise ValueError("output_dir required when write_outputs=True")
        write_audit_outputs(
            output_dir,
            bundle=bundle,
            wall_sequences_csv=str(csv_path or ""),
        )
    return bundle
