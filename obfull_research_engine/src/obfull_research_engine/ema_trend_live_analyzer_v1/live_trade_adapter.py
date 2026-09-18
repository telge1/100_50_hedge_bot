"""Live public-trade fanout → CanonicalTrade (no silent CH fallback)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.canonical_trades import (
    CanonicalTrade,
    canonical_trade_key,
)

from .schema import iso_z, parse_utc


@dataclass
class LiveTradeAdapter:
    """Maps fanout trade events into CanonicalTrade with exchange + receive times."""

    symbol: str
    snapshot_ready_at: datetime
    feature_cutoff_at: datetime | None = None
    coverage_valid: bool = True
    blocked_reason: str | None = None
    trades: list[CanonicalTrade] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    raw_events: list[dict[str, Any]] = field(default_factory=list)

    def ingest_batch(self, events: list[dict[str, Any]]) -> list[CanonicalTrade]:
        out: list[CanonicalTrade] = []
        for ev in events:
            self.raw_events.append(ev)
            if ev.get("overflow") or ev.get("event_type") == "overflow":
                self.coverage_valid = False
                self.blocked_reason = "TRADE_QUEUE_OVERFLOW"
                continue
            if not ev.get("valid_for_analysis", True):
                continue
            tid = str(ev.get("trade_id") or "").strip()
            sym = str(ev.get("symbol") or self.symbol).upper()
            if not tid:
                continue
            key = canonical_trade_key(sym, tid)
            if key in self.seen_ids:
                continue
            exch = parse_utc(ev.get("exchange_event_time"))
            if exch is None:
                continue
            if exch < self.snapshot_ready_at:
                continue
            if self.feature_cutoff_at is not None and exch > self.feature_cutoff_at:
                continue
            recv_ns = ev.get("receive_time_ns")
            recv_iso = None
            avail = exch
            if recv_ns is not None:
                recv_dt = datetime.fromtimestamp(int(recv_ns) / 1e9, tz=timezone.utc)
                recv_iso = iso_z(recv_dt)
                avail = recv_dt
            self.seen_ids.add(key)
            ct = CanonicalTrade(
                canonical_trade_key=key,
                symbol=sym,
                trade_id=tid,
                exchange_event_time=iso_z(exch),
                collector_received_at=recv_iso,
                event_available_at=iso_z(avail),
                sequence=int(ev["record_ordinal"]) if ev.get("record_ordinal") is not None else None,
                price=float(ev.get("price") or 0),
                size_base=float(ev.get("quantity") or ev.get("size") or 0),
                notional_usdt=float(ev.get("notional") or 0),
                taker_side=str(ev.get("side") or ""),
                is_block_trade=bool(ev.get("is_block_trade") or False),
                source_file="public_trade_event_fanout_v1",
                source_record_id=f"{sym}|{tid}|{ev.get('record_ordinal')}",
                dedup_status="KEPT",
            )
            self.trades.append(ct)
            out.append(ct)
        return out

    def status(self) -> dict[str, Any]:
        return {
            "coverage_valid": self.coverage_valid,
            "blocked_reason": self.blocked_reason,
            "trade_count": len(self.trades),
            "raw_event_count": len(self.raw_events),
        }
