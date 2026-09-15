"""Bronze baseline replay using FullBookState + Silver book_map_sha256 semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome

from ..execution_hold import DEFAULT_SILVER_LOCK, assert_full_run_allowed
from ..models import BaselineBookState
from ..time_windows import ns_to_dt
from obfull_research_engine.clickhouse_research_store_v1.silver_replay import (
    book_map_sha256,
)


@dataclass(frozen=True)
class BronzeRecordFull:
    """Fixture/live-normalized bronze row for offline FullBook replay."""

    canonical_segment_chain_index: int
    record_ordinal: int
    event_time_ns: int
    message_type: str  # snapshot | checkpoint | delta | gap_marker
    bids: list[Any] = field(default_factory=list)
    asks: list[Any] = field(default_factory=list)
    u: int | None = None
    seq: int | None = None
    gap: bool = False


@dataclass(frozen=True)
class BronzeRecordLite:
    """Legacy single-level fixture record (promoted to FullBook path)."""

    canonical_segment_chain_index: int
    record_ordinal: int
    event_time_ns: int
    message_type: str
    bids: list[Any] | None = None
    asks: list[Any] | None = None
    side: str | None = None
    price: float | None = None
    size: float | None = None
    u: int | None = None
    seq: int | None = None
    gap: bool = False


def sort_key(rec: BronzeRecordFull) -> tuple[int, int]:
    return (int(rec.canonical_segment_chain_index), int(rec.record_ordinal))


def promote_record(rec: Any) -> BronzeRecordFull:
    if isinstance(rec, BronzeRecordFull):
        return rec
    if getattr(rec, "gap", False) or getattr(rec, "message_type", "") == "gap_marker":
        return BronzeRecordFull(
            int(rec.canonical_segment_chain_index),
            int(rec.record_ordinal),
            int(rec.event_time_ns),
            "gap_marker",
            gap=True,
        )
    mt = str(rec.message_type)
    if mt in {"snapshot", "checkpoint"}:
        return BronzeRecordFull(
            int(rec.canonical_segment_chain_index),
            int(rec.record_ordinal),
            int(rec.event_time_ns),
            mt,
            bids=list(getattr(rec, "bids", None) or []),
            asks=list(getattr(rec, "asks", None) or []),
            u=getattr(rec, "u", None) or 1,
            seq=getattr(rec, "seq", None) or 1,
        )
    bids, asks = list(getattr(rec, "bids", None) or []), list(
        getattr(rec, "asks", None) or []
    )
    if not bids and not asks:
        side = getattr(rec, "side", None)
        price = getattr(rec, "price", None)
        size = getattr(rec, "size", None)
        if side == "bid" and price is not None:
            bids = [[float(price), float(size or 0.0)]]
        if side == "ask" and price is not None:
            asks = [[float(price), float(size or 0.0)]]
    return BronzeRecordFull(
        int(rec.canonical_segment_chain_index),
        int(rec.record_ordinal),
        int(rec.event_time_ns),
        "delta" if mt == "delta" else mt,
        bids=bids,
        asks=asks,
        u=getattr(rec, "u", None),
        seq=getattr(rec, "seq", None),
    )


def replay_baseline_fullbook(
    *,
    symbol: str,
    records: Sequence[BronzeRecordFull],
    start_ns: int,
    expected_book_hash: str | None = None,
    enforce_continuity: bool = True,
) -> BaselineBookState:
    """Apply snapshot then deltas with event_time < start_ns."""
    ordered = sorted(records, key=sort_key)
    state = FullBookState(symbol=symbol.upper())
    last_ns: int | None = None
    saw_anchor = False
    gap_seen = False
    next_u: int | None = None

    for rec in ordered:
        if rec.gap or rec.message_type == "gap_marker":
            gap_seen = True
            continue
        if int(rec.event_time_ns) >= int(start_ns):
            break
        if rec.message_type in {"snapshot", "checkpoint"}:
            if not rec.bids or not rec.asks:
                return BaselineBookState(
                    source="bronze_replay_from_epoch_anchor",
                    timestamp=None,
                    age_ms=None,
                    complete=False,
                    hash=None,
                    unresolved_reason="UNRESOLVED_BASELINE",
                )
            u = rec.u if rec.u is not None else 1
            state.apply_snapshot(
                bids=list(rec.bids),
                asks=list(rec.asks),
                u=u,
                seq=rec.seq,
                ts_ms=int(rec.event_time_ns // 1_000_000),
                mark_ready=True,
            )
            next_u = int(u) + 1
            saw_anchor = True
            last_ns = int(rec.event_time_ns)
            continue
        if not saw_anchor:
            continue
        if rec.message_type != "delta":
            continue
        u = rec.u if rec.u is not None else next_u
        outcome = state.apply_delta(
            bids=list(rec.bids or []),
            asks=list(rec.asks or []),
            u=u,
            seq=rec.seq,
            ts_ms=int(rec.event_time_ns // 1_000_000),
            enforce_continuity=enforce_continuity,
        )
        if enforce_continuity and outcome is not DeltaOutcome.APPLIED:
            return BaselineBookState(
                source="bronze_replay_from_epoch_anchor",
                timestamp=ns_to_dt(int(rec.event_time_ns)),
                age_ms=(start_ns - int(rec.event_time_ns)) / 1e6,
                complete=False,
                hash=None,
                unresolved_reason="UNRESOLVED_BASELINE",
            )
        if u is not None:
            next_u = int(u) + 1
        last_ns = int(rec.event_time_ns)

    if gap_seen or not saw_anchor or last_ns is None:
        return BaselineBookState(
            source="bronze_replay_from_epoch_anchor",
            timestamp=None if last_ns is None else ns_to_dt(last_ns),
            age_ms=None if last_ns is None else (start_ns - last_ns) / 1e6,
            complete=False,
            hash=None if not saw_anchor else book_map_sha256(state.bids, state.asks),
            unresolved_reason="UNRESOLVED_BASELINE",
        )

    digest = book_map_sha256(state.bids, state.asks)
    if expected_book_hash is not None and digest != expected_book_hash:
        return BaselineBookState(
            source="bronze_replay_from_epoch_anchor",
            timestamp=ns_to_dt(last_ns),
            age_ms=(start_ns - last_ns) / 1e6,
            complete=False,
            hash=digest,
            unresolved_reason="UNRESOLVED_BASELINE",
        )

    levels = tuple(
        {"side": "bid", "price": p, "size": s} for p, s in sorted(state.bids.items())
    ) + tuple(
        {"side": "ask", "price": p, "size": s} for p, s in sorted(state.asks.items())
    )
    return BaselineBookState(
        source="bronze_replay_from_epoch_anchor",
        timestamp=ns_to_dt(last_ns),
        age_ms=(start_ns - last_ns) / 1e6,
        complete=True,
        hash=digest,
        unresolved_reason=None,
        levels_in_band=levels,
    )


class FixtureBaselineBookRepository:
    def __init__(
        self,
        records: Sequence[Any],
        *,
        enforce_continuity: bool = True,
        symbol_default: str = "BTCUSDT",
    ):
        self.records = [promote_record(r) for r in records]
        self.enforce_continuity = enforce_continuity
        self.symbol_default = symbol_default

    def load_baseline(
        self,
        *,
        symbol: str,
        start_ns: int,
        epoch_id: str,
        expected_book_hash: str | None,
    ) -> BaselineBookState:
        return replay_baseline_fullbook(
            symbol=symbol or self.symbol_default,
            records=self.records,
            start_ns=start_ns,
            expected_book_hash=expected_book_hash,
            enforce_continuity=self.enforce_continuity,
        )


class LiveBaselineBookRepository:
    def __init__(self, lock_path=DEFAULT_SILVER_LOCK):
        self.lock_path = lock_path

    def load_baseline(
        self,
        *,
        symbol: str,
        start_ns: int,
        epoch_id: str,
        expected_book_hash: str | None,
    ) -> BaselineBookState:
        assert_full_run_allowed(self.lock_path)
        raise RuntimeError(
            "STOP_XRAY_LIVE_BRONZE_NOT_ENABLED_UNDER_EXECUTION_HOLD: "
            "full Bronze CH load reserved for post-hold runs"
        )


def replay_baseline_from_records(
    *,
    records: Sequence[Any],
    start_ns: int,
    expected_book_hash: str | None = None,
    hash_fn=None,
    symbol: str = "BTCUSDT",
    enforce_continuity: bool = False,
) -> BaselineBookState:
    """Public helper; legacy fixtures often omit continuous ``u`` so default off."""
    full = [promote_record(r) for r in records]
    # Auto-assign contiguous u when missing (legacy lite fixtures)
    u = 1
    fixed: list[BronzeRecordFull] = []
    for r in full:
        if r.message_type in {"snapshot", "checkpoint"}:
            uu = r.u if r.u is not None else u
            fixed.append(
                BronzeRecordFull(
                    r.canonical_segment_chain_index,
                    r.record_ordinal,
                    r.event_time_ns,
                    r.message_type,
                    bids=r.bids,
                    asks=r.asks,
                    u=uu,
                    seq=r.seq or 1,
                )
            )
            u = int(uu) + 1
        elif r.message_type == "delta":
            uu = r.u if r.u is not None else u
            fixed.append(
                BronzeRecordFull(
                    r.canonical_segment_chain_index,
                    r.record_ordinal,
                    r.event_time_ns,
                    "delta",
                    bids=r.bids,
                    asks=r.asks,
                    u=uu,
                    seq=r.seq,
                )
            )
            u = int(uu) + 1
        else:
            fixed.append(r)
    return replay_baseline_fullbook(
        symbol=symbol,
        records=fixed,
        start_ns=start_ns,
        expected_book_hash=expected_book_hash,
        enforce_continuity=enforce_continuity,
    )
