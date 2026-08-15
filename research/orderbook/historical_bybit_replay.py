"""Historical Bybit linear orderbook.200 NDJSON replay (research layer).

Reconstructs the causal L2 book at a target timestamp from daily
``YYYY-MM-DD_SYMBOL_ob200.data`` files under ``data/bybit_historical_orderbook/``.

Causality boundary
------------------
Replay cutoff uses message field ``ts`` (exchange/stream timestamp, ms UTC),
not ``cts`` (matching-engine timestamp).

Reason: for historical reconstruction of "what the public orderbook stream
had published by wall time T", ``ts`` is the time associated with the
published message. ``cts`` is typically a few ms earlier and is retained for
diagnostics only. No message with ``ts > target_ts_ms`` is applied.

Delta semantics (Bybit)
-----------------------
- snapshot: replace entire book
- delta: for each [price, qty], qty>0 upserts level; qty==0 deletes level
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "bybit_historical_orderbook"


class ReplayError(RuntimeError):
    """Unrecoverable historical replay failure."""


class SequenceStatus(str, Enum):
    CLEAN = "CLEAN"
    DUPLICATES_SEEN = "DUPLICATES_SEEN"
    RESET_SEEN = "RESET_SEEN"
    POSSIBLE_GAP = "POSSIBLE_GAP"
    INVALID = "INVALID"


@dataclass(frozen=True)
class ObMessage:
    """One NDJSON orderbook.200 message."""

    message_type: str  # snapshot | delta
    ts_ms: int
    cts_ms: int | None
    symbol: str
    update_id: int  # data.u
    cross_sequence: int  # data.seq
    bids: tuple[tuple[Decimal, Decimal], ...]  # (price, qty)
    asks: tuple[tuple[Decimal, Decimal], ...]
    source_line: int
    topic: str | None = None


@dataclass
class SequenceDiagnostics:
    snapshots_seen: int = 0
    deltas_applied: int = 0
    duplicate_u_count: int = 0
    u_backward_count: int = 0
    u_gap_count: int = 0
    seq_backward_count: int = 0
    ts_backward_count: int = 0
    cts_backward_count: int = 0
    midstream_snapshot_resets: int = 0
    malformed_lines: int = 0
    notes: list[str] = field(default_factory=list)

    def status(self) -> SequenceStatus:
        if self.u_backward_count > 0 and self.midstream_snapshot_resets == 0:
            # Backward u without an accompanying snapshot reset is invalid.
            return SequenceStatus.INVALID
        if self.duplicate_u_count > 0:
            return SequenceStatus.DUPLICATES_SEEN
        if self.midstream_snapshot_resets > 0:
            return SequenceStatus.RESET_SEEN
        if self.u_gap_count > 0:
            return SequenceStatus.POSSIBLE_GAP
        return SequenceStatus.CLEAN


@dataclass
class InvariantReport:
    ok: bool
    best_bid_lt_best_ask: bool
    no_nonpositive_qty: bool
    no_duplicate_prices: bool  # always true with dict keys; kept explicit
    book_nonempty: bool
    last_applied_le_target: bool
    details: list[str] = field(default_factory=list)


@dataclass
class ReplayResult:
    symbol: str
    date: str
    target_ts_ms: int
    cutoff_field: str
    last_applied_message_ts_ms: int | None
    last_applied_message_cts_ms: int | None
    last_snapshot_ts_ms: int | None
    last_update_id: int | None
    last_seq: int | None
    deltas_applied: int
    messages_applied: int
    bid_levels: list[tuple[str, str]]  # price, qty as strings
    ask_levels: list[tuple[str, str]]
    best_bid: str | None
    best_ask: str | None
    spread: str | None
    bid_level_count: int
    ask_level_count: int
    sequence_status: SequenceStatus
    sequence_diagnostics: SequenceDiagnostics
    invariants: InvariantReport
    file_path: str

    def top_n(self, n: int = 10) -> dict[str, list[tuple[str, str]]]:
        return {
            "bids": self.bid_levels[:n],
            "asks": self.ask_levels[:n],
        }

    def fingerprint(self, top_n: int = 20) -> dict[str, Any]:
        return {
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "bid_level_count": self.bid_level_count,
            "ask_level_count": self.ask_level_count,
            "last_update_id": self.last_update_id,
            "last_seq": self.last_seq,
            "top_bids": self.bid_levels[:top_n],
            "top_asks": self.ask_levels[:top_n],
            "last_applied_message_ts_ms": self.last_applied_message_ts_ms,
            "deltas_applied": self.deltas_applied,
        }


class OrderBook:
    """Mutable L2 book with Decimal price keys."""

    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_update_id: int | None = None
        self.last_seq: int | None = None
        self.has_snapshot: bool = False
        self.last_message_ts_ms: int | None = None
        self.last_message_cts_ms: int | None = None
        self.last_snapshot_ts_ms: int | None = None

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self.last_seq = None
        self.has_snapshot = False
        # keep last_* timestamps unset until next apply
        self.last_message_ts_ms = None
        self.last_message_cts_ms = None
        self.last_snapshot_ts_ms = None

    def apply_snapshot(self, msg: ObMessage) -> None:
        self.bids.clear()
        self.asks.clear()
        for price, qty in msg.bids:
            if qty > 0:
                self.bids[price] = qty
        for price, qty in msg.asks:
            if qty > 0:
                self.asks[price] = qty
        self.last_update_id = msg.update_id
        self.last_seq = msg.cross_sequence
        self.has_snapshot = True
        self.last_message_ts_ms = msg.ts_ms
        self.last_message_cts_ms = msg.cts_ms
        self.last_snapshot_ts_ms = msg.ts_ms

    def apply_delta(self, msg: ObMessage) -> None:
        if not self.has_snapshot:
            raise ReplayError(
                f"delta before snapshot at line {msg.source_line} u={msg.update_id}"
            )
        for price, qty in msg.bids:
            if qty == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty
        for price, qty in msg.asks:
            if qty == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty
        self.last_update_id = msg.update_id
        self.last_seq = msg.cross_sequence
        self.last_message_ts_ms = msg.ts_ms
        self.last_message_cts_ms = msg.cts_ms

    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def spread(self) -> Decimal | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return ba - bb

    def sorted_bids(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self.bids.items(), key=lambda x: x[0], reverse=True)

    def sorted_asks(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self.asks.items(), key=lambda x: x[0])


def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _as_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ReplayError(f"invalid decimal: {value!r}") from exc


def _parse_levels(raw: Any) -> tuple[tuple[Decimal, Decimal], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ReplayError(f"levels must be list, got {type(raw).__name__}")
    out: list[tuple[Decimal, Decimal]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            raise ReplayError(f"invalid level row: {item!r}")
        out.append((_as_decimal(item[0]), _as_decimal(item[1])))
    return tuple(out)


def parse_ob_line(line: str, *, source_line: int, expected_symbol: str | None = None) -> ObMessage:
    text = line.strip()
    if not text:
        raise ReplayError(f"empty line {source_line}")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"malformed JSON at line {source_line}: {exc}") from exc
    if not isinstance(obj, dict):
        raise ReplayError(f"line {source_line}: expected object")

    msg_type = str(obj.get("type") or "")
    if msg_type not in {"snapshot", "delta"}:
        raise ReplayError(f"line {source_line}: unsupported type={msg_type!r}")

    try:
        ts_ms = int(obj["ts"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayError(f"line {source_line}: missing/invalid ts") from exc

    cts_raw = obj.get("cts")
    cts_ms = int(cts_raw) if cts_raw is not None else None

    data = obj.get("data")
    if not isinstance(data, dict):
        raise ReplayError(f"line {source_line}: missing data object")

    symbol = str(data.get("s") or "")
    if not symbol:
        # fallback from topic orderbook.200.SYMBOL
        topic = str(obj.get("topic") or "")
        if topic.startswith("orderbook.200."):
            symbol = topic.split("orderbook.200.", 1)[1]
    if expected_symbol and symbol and symbol != expected_symbol:
        raise ReplayError(
            f"line {source_line}: symbol mismatch {symbol!r} != {expected_symbol!r}"
        )
    if not symbol:
        raise ReplayError(f"line {source_line}: missing symbol")

    try:
        update_id = int(data["u"])
        cross_sequence = int(data["seq"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayError(f"line {source_line}: missing/invalid u/seq") from exc

    return ObMessage(
        message_type=msg_type,
        ts_ms=ts_ms,
        cts_ms=cts_ms,
        symbol=symbol,
        update_id=update_id,
        cross_sequence=cross_sequence,
        bids=_parse_levels(data.get("b")),
        asks=_parse_levels(data.get("a")),
        source_line=source_line,
        topic=str(obj.get("topic")) if obj.get("topic") is not None else None,
    )


def day_file_path(
    symbol: str,
    date: str,
    *,
    data_root: Path | None = None,
) -> Path:
    root = data_root or DEFAULT_DATA_ROOT
    return root / symbol / date / f"{date}_{symbol}_ob200.data"


def iter_messages(
    path: Path,
    *,
    expected_symbol: str | None = None,
    skip_malformed: bool = False,
) -> Iterator[ObMessage | tuple[int, str]]:
    """Yield ObMessage; if skip_malformed, yield (line_no, error) tuples for bad lines."""
    if not path.exists():
        raise ReplayError(f"missing orderbook file: {path}")
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                yield parse_ob_line(
                    line, source_line=line_no, expected_symbol=expected_symbol
                )
            except ReplayError as exc:
                if skip_malformed:
                    yield (line_no, str(exc))
                else:
                    raise


def _dec_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _levels_as_str_pairs(levels: Sequence[tuple[Decimal, Decimal]]) -> list[tuple[str, str]]:
    return [(format(p, "f"), format(q, "f")) for p, q in levels]


def check_invariants(
    book: OrderBook,
    *,
    target_ts_ms: int,
) -> InvariantReport:
    details: list[str] = []
    bb, ba = book.best_bid(), book.best_ask()
    crossed_ok = bb is not None and ba is not None and bb < ba
    if not crossed_ok:
        details.append(f"best_bid={bb} best_ask={ba} not strictly ordered")

    nonpos = False
    for side_name, side in (("bid", book.bids), ("ask", book.asks)):
        for price, qty in side.items():
            if qty <= 0:
                nonpos = True
                details.append(f"nonpositive qty {side_name} {price}={qty}")
                break
        if nonpos:
            break
    no_nonpositive = not nonpos

    nonempty = bool(book.bids) and bool(book.asks)
    if not nonempty:
        details.append("book empty on one or both sides")

    last_ok = (
        book.last_message_ts_ms is not None and book.last_message_ts_ms <= target_ts_ms
    )
    if not last_ok:
        details.append(
            f"last_applied_ts={book.last_message_ts_ms} > target={target_ts_ms}"
        )

    ok = crossed_ok and no_nonpositive and nonempty and last_ok
    return InvariantReport(
        ok=ok,
        best_bid_lt_best_ask=crossed_ok,
        no_nonpositive_qty=no_nonpositive,
        no_duplicate_prices=True,
        book_nonempty=nonempty,
        last_applied_le_target=last_ok,
        details=details,
    )


class HistoricalBybitReplayer:
    """Streaming causal replayer for one symbol/day file."""

    def __init__(self) -> None:
        self.book = OrderBook()
        self.diag = SequenceDiagnostics()
        self._prev_u: int | None = None
        self._prev_seq: int | None = None
        self._prev_ts: int | None = None
        self._prev_cts: int | None = None
        self._messages_applied = 0

    def _observe_sequence(self, msg: ObMessage, *, is_first_snapshot: bool) -> None:
        if self._prev_ts is not None and msg.ts_ms < self._prev_ts:
            self.diag.ts_backward_count += 1
            self.diag.notes.append(
                f"ts backward line={msg.source_line}: {self._prev_ts}->{msg.ts_ms}"
            )
        if (
            self._prev_cts is not None
            and msg.cts_ms is not None
            and msg.cts_ms < self._prev_cts
        ):
            self.diag.cts_backward_count += 1

        if msg.message_type == "snapshot":
            self.diag.snapshots_seen += 1
            if not is_first_snapshot:
                self.diag.midstream_snapshot_resets += 1
                self.diag.notes.append(
                    f"midstream snapshot reset line={msg.source_line} u={msg.update_id}"
                )
        else:
            if self._prev_u is not None:
                if msg.update_id == self._prev_u:
                    self.diag.duplicate_u_count += 1
                    self.diag.notes.append(
                        f"duplicate u={msg.update_id} line={msg.source_line}"
                    )
                elif msg.update_id < self._prev_u:
                    self.diag.u_backward_count += 1
                    self.diag.notes.append(
                        f"u backward {self._prev_u}->{msg.update_id} line={msg.source_line}"
                    )
                elif msg.update_id > self._prev_u + 1:
                    self.diag.u_gap_count += 1
                    self.diag.notes.append(
                        f"u gap expected {self._prev_u + 1} got {msg.update_id} "
                        f"line={msg.source_line}"
                    )

        if self._prev_seq is not None and msg.cross_sequence < self._prev_seq:
            self.diag.seq_backward_count += 1
            self.diag.notes.append(
                f"seq backward {self._prev_seq}->{msg.cross_sequence} line={msg.source_line}"
            )

        self._prev_u = msg.update_id
        self._prev_seq = msg.cross_sequence
        self._prev_ts = msg.ts_ms
        if msg.cts_ms is not None:
            self._prev_cts = msg.cts_ms

    def apply_message(self, msg: ObMessage) -> None:
        is_first_snapshot = (
            msg.message_type == "snapshot" and self.diag.snapshots_seen == 0
        )
        self._observe_sequence(msg, is_first_snapshot=is_first_snapshot)
        if msg.message_type == "snapshot":
            self.book.apply_snapshot(msg)
        elif msg.message_type == "delta":
            self.book.apply_delta(msg)
            self.diag.deltas_applied += 1
        else:
            raise ReplayError(f"unsupported type={msg.message_type!r}")
        self._messages_applied += 1

    def replay_to(
        self,
        path: Path,
        *,
        symbol: str,
        date: str,
        target_ts_ms: int,
        skip_malformed: bool = True,
    ) -> ReplayResult:
        self.book = OrderBook()
        self.diag = SequenceDiagnostics()
        self._prev_u = None
        self._prev_seq = None
        self._prev_ts = None
        self._prev_cts = None
        self._messages_applied = 0

        saw_applicable = False
        for item in iter_messages(
            path, expected_symbol=symbol, skip_malformed=skip_malformed
        ):
            if isinstance(item, tuple):
                self.diag.malformed_lines += 1
                self.diag.notes.append(f"malformed line {item[0]}: {item[1]}")
                continue
            msg = item
            if msg.ts_ms > target_ts_ms:
                break
            saw_applicable = True
            self.apply_message(msg)

        if not saw_applicable:
            raise ReplayError(
                f"no messages with ts <= target_ts_ms={target_ts_ms} in {path}"
            )
        if not self.book.has_snapshot:
            raise ReplayError(
                f"no snapshot applied before target_ts_ms={target_ts_ms} in {path}"
            )

        bids = self.book.sorted_bids()
        asks = self.book.sorted_asks()
        invariants = check_invariants(self.book, target_ts_ms=target_ts_ms)
        seq_status = self.diag.status()
        if not invariants.ok:
            seq_status = SequenceStatus.INVALID

        return ReplayResult(
            symbol=symbol,
            date=date,
            target_ts_ms=target_ts_ms,
            cutoff_field="ts",
            last_applied_message_ts_ms=self.book.last_message_ts_ms,
            last_applied_message_cts_ms=self.book.last_message_cts_ms,
            last_snapshot_ts_ms=self.book.last_snapshot_ts_ms,
            last_update_id=self.book.last_update_id,
            last_seq=self.book.last_seq,
            deltas_applied=self.diag.deltas_applied,
            messages_applied=self._messages_applied,
            bid_levels=_levels_as_str_pairs(bids),
            ask_levels=_levels_as_str_pairs(asks),
            best_bid=_dec_str(self.book.best_bid()),
            best_ask=_dec_str(self.book.best_ask()),
            spread=_dec_str(self.book.spread()),
            bid_level_count=len(bids),
            ask_level_count=len(asks),
            sequence_status=seq_status,
            sequence_diagnostics=self.diag,
            invariants=invariants,
            file_path=str(path),
        )


def replay_symbol_day(
    symbol: str,
    date: str,
    target_ts_ms: int,
    *,
    data_root: Path | None = None,
    skip_malformed: bool = True,
) -> ReplayResult:
    path = day_file_path(symbol, date, data_root=data_root)
    return HistoricalBybitReplayer().replay_to(
        path,
        symbol=symbol,
        date=date,
        target_ts_ms=target_ts_ms,
        skip_malformed=skip_malformed,
    )


def apply_levels_trace(
    book: OrderBook,
    msg: ObMessage,
    *,
    prices_of_interest: Sequence[Decimal] | None = None,
) -> list[dict[str, Any]]:
    """Apply one message and return before/delta/after traces for touched prices."""
    interest = set(prices_of_interest or [])
    for p, _ in msg.bids:
        interest.add(p)
    for p, _ in msg.asks:
        interest.add(p)

    before: dict[tuple[str, Decimal], Decimal | None] = {}
    for price in interest:
        before[("bid", price)] = book.bids.get(price)
        before[("ask", price)] = book.asks.get(price)

    if msg.message_type == "snapshot":
        book.apply_snapshot(msg)
    else:
        book.apply_delta(msg)

    rows: list[dict[str, Any]] = []
    touched: list[tuple[str, Decimal, Decimal]] = [
        ("bid", p, q) for p, q in msg.bids
    ] + [("ask", p, q) for p, q in msg.asks]
    # For snapshot, show interest prices even if not "touched" list limited
    if msg.message_type == "snapshot":
        for price in sorted(interest):
            for side in ("bid", "ask"):
                side_map = book.bids if side == "bid" else book.asks
                rows.append(
                    {
                        "side": side,
                        "price": format(price, "f"),
                        "before": _dec_str(before[(side, price)]),
                        "delta_qty": "snapshot",
                        "after": _dec_str(side_map.get(price)),
                    }
                )
        return rows

    for side, price, qty in touched:
        side_map = book.bids if side == "bid" else book.asks
        rows.append(
            {
                "side": side,
                "price": format(price, "f"),
                "before": _dec_str(before[(side, price)]),
                "delta_qty": format(qty, "f"),
                "after": _dec_str(side_map.get(price)),
            }
        )
    return rows
