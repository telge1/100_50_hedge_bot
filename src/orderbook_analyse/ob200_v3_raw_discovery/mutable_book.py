"""In-place causal book for OB200 discovery (avoids per-delta dict copies)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

ZERO = Decimal("0")


class MutableBook:
    __slots__ = ("bids", "asks", "last_u", "last_seq", "is_valid")

    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_u: int = 0
        self.last_seq: int = 0
        self.is_valid: bool = False

    def clear_invalid(self, *, last_u: int, last_seq: int) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_u = last_u
        self.last_seq = last_seq
        self.is_valid = False

    def apply_snapshot(self, data: dict[str, Any]) -> None:
        self.bids.clear()
        self.asks.clear()
        for item in data.get("b") or []:
            price = Decimal(item[0])
            qty = Decimal(item[1])
            if qty > ZERO:
                self.bids[price] = qty
        for item in data.get("a") or []:
            price = Decimal(item[0])
            qty = Decimal(item[1])
            if qty > ZERO:
                self.asks[price] = qty
        self.last_u = int(data.get("u") or 0)
        self.last_seq = int(data.get("seq") or 0)
        self.is_valid = True

    def apply_delta(self, data: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        new_u = int(data.get("u") or 0)
        new_seq = int(data.get("seq") or 0)
        if not self.is_valid:
            self.clear_invalid(last_u=new_u, last_seq=new_seq)
            return ["gap_propagated"]
        if new_u != self.last_u + 1:
            if new_u == self.last_u:
                warnings.append(f"seq_dup:u={new_u}")
                return warnings
            warnings.append(f"seq_gap:prev={self.last_u},cur={new_u}")
            self.clear_invalid(last_u=new_u, last_seq=new_seq)
            return warnings
        for item in data.get("b") or []:
            price = Decimal(item[0])
            qty = Decimal(item[1])
            if qty == ZERO:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty
        for item in data.get("a") or []:
            price = Decimal(item[0])
            qty = Decimal(item[1])
            if qty == ZERO:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty
        self.last_u = new_u
        self.last_seq = new_seq
        return warnings

    def sorted_bids(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self.bids.items(), key=lambda x: x[0], reverse=True)

    def sorted_asks(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self.asks.items(), key=lambda x: x[0])

    def is_crossed(self) -> bool:
        if not self.bids or not self.asks:
            return False
        return max(self.bids) >= min(self.asks)

    def end_fingerprint(self) -> tuple[int, int, str | None, str | None, str | None, str | None]:
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        return (
            len(self.bids),
            len(self.asks),
            None if bb is None else str(bb),
            None if ba is None else str(ba),
            None if bb is None else str(self.bids[bb]),
            None if ba is None else str(self.asks[ba]),
        )
