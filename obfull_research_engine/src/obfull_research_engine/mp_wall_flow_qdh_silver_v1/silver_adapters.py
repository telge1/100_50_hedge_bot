"""Adapt Silver LC / metrics / public trades into QDH package inputs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from obfull_research_engine.breakout_xray_v1.ports import LevelChangeEvent
from obfull_research_engine.breakout_xray_v1.trades import XRayTrade
from obfull_research_engine.drilldown.aggregation_100ms import _as_dt, _floor_bucket
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1 import BUCKET_MS
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.canonical_trades import (
    build_canonical_trades,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.loaders import MetricTick
from obfull_research_engine.mp_ob_feature_enrichment_v1.params import NS
from obfull_research_engine.timeparse import format_utc_z


def ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)


def dt_to_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)


def level_changes_to_qdh_rows(events: Sequence[LevelChangeEvent]) -> list[dict[str, Any]]:
    """Map Silver LevelChangeEvent → dicts expected by wall_flow_attribution."""
    rows: list[dict[str, Any]] = []
    for ev in events:
        exch = ns_to_dt(int(ev.event_time_ns))
        # Research proxy: available at ceil-100ms of exchange time (same as Episode-1 persist).
        avail = _floor_bucket(exch, BUCKET_MS) + timedelta(milliseconds=BUCKET_MS)
        rows.append(
            {
                "event_time": format_utc_z(exch),
                "event_available_at": format_utc_z(avail),
                "side": str(ev.side).lower(),
                "price": float(ev.price),
                "change_type": str(ev.change_type),
                "old_size": None if ev.old_size is None else float(ev.old_size),
                "new_size": float(ev.new_size),
                "apply_order": ev.apply_order,
                "source_event_id": (
                    None
                    if not ev.record_provenance
                    else str(ev.record_provenance.get("source_record_id") or "")
                )
                or f"lc:{ev.event_time_ns}:{ev.apply_order}",
                "u": (
                    None
                    if not ev.record_provenance
                    else ev.record_provenance.get("update_id")
                ),
                "seq": (
                    None
                    if not ev.record_provenance
                    else ev.record_provenance.get("seq")
                ),
                "replay_epoch": None,
            }
        )
    return rows


def seed_initial_asks_from_level_changes(
    rows: Sequence[dict[str, Any]],
    *,
    band_low: float,
    band_high: float,
    wall_side: str,
    coverage_start: datetime,
) -> dict[float, float]:
    """Seed band book at coverage_start from LC history.

    - Apply all LCs before coverage_start (new_size).
    - For each price first seen at/after coverage_start, seed with old_size.
    """
    book: dict[float, float] = {}
    ordered = sorted(
        rows,
        key=lambda r: (
            _as_dt(r["event_time"]).timestamp(),
            int(r.get("apply_order") or 0),
        ),
    )
    seen_after: set[float] = set()
    for r in ordered:
        if str(r.get("side")) != wall_side:
            continue
        px = float(r["price"])
        if not (band_low - 1e-12 <= px <= band_high + 1e-12):
            continue
        et = _as_dt(r["event_time"])
        new_s = float(r["new_size"])
        if et < coverage_start:
            if new_s > 0:
                book[px] = new_s
            else:
                book.pop(px, None)
            continue
        if px in seen_after:
            continue
        seen_after.add(px)
        old = r.get("old_size")
        if old is not None:
            book[px] = float(old)
        elif px not in book:
            book[px] = 0.0
    return {px: qty for px, qty in book.items() if qty and qty > 0}


def metrics_to_qdh_states(ticks: Sequence[MetricTick]) -> list[dict[str, Any]]:
    """Map Silver 100ms metrics → feature-timeline state rows."""
    out: list[dict[str, Any]] = []
    for t in ticks:
        bstart = ns_to_dt(int(t.bucket_start_ns))
        bend = bstart + timedelta(milliseconds=BUCKET_MS)
        out.append(
            {
                "bucket_start": format_utc_z(bstart),
                "bucket_end_exclusive": format_utc_z(bend),
                "available_at": format_utc_z(bend),  # research proxy
                "best_bid": t.best_bid,
                "best_ask": t.best_ask,
                "midprice": t.mid,
                "bid_depth_2bps": t.bid_depth_near,
                "ask_depth_2bps": t.ask_depth_near,
                "n_bid_levels": None,
                "n_ask_levels": None,
                "replay_epoch": None,
            }
        )
    return out


def xray_trades_to_raw_rows(trades: Sequence[XRayTrade], *, symbol: str) -> list[dict[str, Any]]:
    """Convert XRayTrade → raw dicts for build_canonical_trades."""
    rows: list[dict[str, Any]] = []
    for tr in trades:
        ts = tr.trade_ts
        if not isinstance(ts, datetime):
            continue
        ts = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        rows.append(
            {
                "symbol": symbol,
                "trade_id": str(tr.trade_id),
                "trade_ts": format_utc_z(ts),
                "exchange_event_time": format_utc_z(ts),
                "price": float(tr.price),
                "size": float(tr.size),
                "qty": float(tr.size),
                "taker_side": str(tr.side),
                "side": str(tr.side),
                "is_block_trade": False,
                "collector_received_at": None,
                "ingest_timestamp": None,
            }
        )
    return rows


def build_trades_for_qdh(
    trades: Sequence[XRayTrade], *, symbol: str, source_file: str
) -> tuple[list[Any], Any, list[Any]]:
    raw = xray_trades_to_raw_rows(trades, symbol=symbol)
    return build_canonical_trades(raw, symbol=symbol, source_file=source_file)


__all__ = [
    "level_changes_to_qdh_rows",
    "seed_initial_asks_from_level_changes",
    "metrics_to_qdh_states",
    "build_trades_for_qdh",
    "ns_to_dt",
    "dt_to_ns",
]
