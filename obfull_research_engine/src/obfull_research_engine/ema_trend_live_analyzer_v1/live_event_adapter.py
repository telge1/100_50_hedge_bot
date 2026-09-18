"""Pure schema adapter: fanout events -> BookNode / level changes. No domain formulas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    BookNode,
)

from .schema import as_utc, iso_z, parse_utc


def _sort_key(ev: dict[str, Any]) -> tuple:
    gen = int(ev.get("snapshot_generation") or 0)
    seq = ev.get("sequence_id")
    uid = ev.get("update_id")
    ord_ = int(ev.get("record_ordinal") or 0)
    # Prefer sequence then update_id; never ms-only
    seq_i = int(seq) if seq is not None else -1
    uid_i = int(uid) if uid is not None else -1
    return (gen, seq_i, uid_i, ord_)


@dataclass
class LiveEventAdapter:
    """Maps fanout level events into canonical BookNode queues at a tracked price."""

    symbol: str
    wall_price: float
    wall_side: str  # bid|ask
    replay_epoch: int = 1
    coverage_valid: bool = True
    exact_features_valid: bool = True
    blocked_reason: str | None = None
    nodes: list[BookNode] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    last_generation: int = 0
    apply_order: int = 0

    def ingest_batch(self, events: list[dict[str, Any]]) -> list[BookNode]:
        if not events:
            return []
        ordered = sorted(events, key=_sort_key)
        new_nodes: list[BookNode] = []
        for ev in ordered:
            self.raw_events.append(ev)
            if ev.get("overflow") or ev.get("event_type") == "overflow":
                self.coverage_valid = False
                self.exact_features_valid = False
                self.blocked_reason = "QUEUE_OVERFLOW"
                continue
            if ev.get("gap") or (ev.get("outcome") in {"gap", "u_reset"}):
                self.coverage_valid = False
                self.exact_features_valid = False
                self.blocked_reason = "SEQUENCE_GAP"
                continue
            if not ev.get("valid_for_analysis", True):
                continue
            gen = int(ev.get("snapshot_generation") or 0)
            if self.last_generation and gen and gen < self.last_generation:
                # stale generation — ignore
                continue
            if gen > self.last_generation:
                self.last_generation = gen
                self.replay_epoch = gen
            side = str(ev.get("side") or "").lower()
            price = ev.get("price")
            if price is None:
                continue
            # Only track exact wall price for BookNode queue timeline (thin wrapper)
            if abs(float(price) - float(self.wall_price)) > 1e-12:
                continue
            if side and side != self.wall_side.lower():
                continue
            qty = ev.get("new_qty")
            if qty is None:
                continue
            self.apply_order += 1
            exch = parse_utc(ev.get("exchange_event_time")) or datetime.now(timezone.utc)
            recv_ns = ev.get("receive_time_ns")
            recv = None
            if recv_ns is not None:
                recv = datetime.fromtimestamp(int(recv_ns) / 1e9, tz=timezone.utc)
            avail = recv or exch
            node = BookNode(
                exchange_event_time=exch,
                available_at=avail,
                collector_received_at=recv,
                queue=float(qty),
                replay_epoch=self.replay_epoch,
                sequence=int(ev["sequence_id"]) if ev.get("sequence_id") is not None else None,
                update_id=int(ev["update_id"]) if ev.get("update_id") is not None else None,
                apply_order=self.apply_order,
                source_event_id=f"{self.symbol}|{ev.get('update_id')}|{ev.get('sequence_id')}",
                source_record_id=f"{self.symbol}|{ev.get('record_ordinal')}|{ev.get('side')}|{price}",
            )
            self.nodes.append(node)
            new_nodes.append(node)
        return new_nodes

    def status(self) -> dict[str, Any]:
        return {
            "coverage_valid": self.coverage_valid,
            "exact_features_valid": self.exact_features_valid,
            "blocked_reason": self.blocked_reason,
            "node_count": len(self.nodes),
            "raw_event_count": len(self.raw_events),
            "replay_epoch": self.replay_epoch,
            "last_generation": self.last_generation,
        }
