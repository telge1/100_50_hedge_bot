"""Chunk-based OB200 v3 hour-archive replay (canonical SoT for BTC OB Fight)."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .config import WALL_MAX_BPS, WALL_QTY_MEDIAN_MULT

ZERO = Decimal("0")


class MutableBook:
    __slots__ = ("bids", "asks", "last_u", "last_seq", "is_valid")

    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_u = 0
        self.last_seq = 0
        self.is_valid = False

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
            price = Decimal(str(item[0]))
            qty = Decimal(str(item[1]))
            if qty > ZERO:
                self.bids[price] = qty
        for item in data.get("a") or []:
            price = Decimal(str(item[0]))
            qty = Decimal(str(item[1]))
            if qty > ZERO:
                self.asks[price] = qty
        self.last_u = int(data.get("u") or 0)
        self.last_seq = int(data.get("seq") or 0)
        self.is_valid = True

    def apply_delta(self, data: dict[str, Any]) -> None:
        new_u = int(data.get("u") or 0)
        new_seq = int(data.get("seq") or 0)
        if not self.is_valid:
            self.clear_invalid(last_u=new_u, last_seq=new_seq)
            return
        if new_u != self.last_u + 1:
            if new_u == self.last_u:
                return
            self.clear_invalid(last_u=new_u, last_seq=new_seq)
            return
        for item in data.get("b") or []:
            price = Decimal(str(item[0]))
            qty = Decimal(str(item[1]))
            if qty == ZERO:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty
        for item in data.get("a") or []:
            price = Decimal(str(item[0]))
            qty = Decimal(str(item[1]))
            if qty == ZERO:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty
        self.last_u = new_u
        self.last_seq = new_seq

    def sorted_bids(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self.bids.items(), key=lambda x: x[0], reverse=True)

    def sorted_asks(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self.asks.items(), key=lambda x: x[0])


def iter_ndjson(path: Path) -> Iterator[dict[str, Any]]:
    import zstandard as zstd

    dctx = zstd.ZstdDecompressor()
    with path.open("rb") as fh:
        with dctx.stream_reader(fh) as reader:
            buf = b""
            while True:
                chunk = reader.read(1 << 20)
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = buf[:nl]
                    buf = buf[nl + 1 :]
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        yield obj


def find_hour_segment(ob_root: Path, symbol: str, at: datetime) -> Path:
    at = at.astimezone(timezone.utc) if at.tzinfo else at.replace(tzinfo=timezone.utc)
    hour = at.replace(minute=0, second=0, microsecond=0)
    end = hour + timedelta(hours=1)
    stamp0 = hour.strftime("%Y%m%dT%H%M%SZ")
    stamp1 = end.strftime("%Y%m%dT%H%M%SZ")
    name = f"{symbol}_{stamp0}_{stamp1}_ob200_v3.zst"
    path = ob_root / symbol / f"{hour.year:04d}" / f"{hour.month:02d}" / f"{hour.day:02d}" / name
    if not path.is_file():
        hits = sorted((ob_root / symbol).rglob(name))
        if not hits:
            raise FileNotFoundError(name)
        path = hits[0]
    return path


def replay_as_of(ob_root: Path, symbol: str, at: datetime) -> dict[str, Any]:
    at = at.astimezone(timezone.utc) if at.tzinfo else at.replace(tzinfo=timezone.utc)
    path = find_hour_segment(ob_root, symbol, at)
    cutoff_ms = int(at.timestamp() * 1000)
    book = MutableBook()
    last_ts = None
    events = 0
    gaps = 0
    for obj in iter_ndjson(path):
        ts = obj.get("ts")
        if isinstance(ts, int) and ts >= cutoff_ms:
            break
        typ = obj.get("type")
        data = obj.get("data") or {}
        if typ in ("snapshot", "rotation_checkpoint"):
            book.apply_snapshot(data)
        elif typ == "delta":
            prev_valid = book.is_valid
            prev_u = book.last_u
            book.apply_delta(data)
            if prev_valid and not book.is_valid and int(data.get("u") or 0) != prev_u:
                gaps += 1
        else:
            continue
        if isinstance(ts, int):
            last_ts = ts
        events += 1
    if not book.is_valid or not book.bids or not book.asks:
        raise RuntimeError(f"invalid book at {at.isoformat()} segment={path}")
    bids = book.sorted_bids()
    asks = book.sorted_asks()
    bb, ba = bids[0][0], asks[0][0]
    if bb >= ba:
        raise RuntimeError("crossed book")
    mid = (bb + ba) / 2
    book_ts = (
        datetime.fromtimestamp(last_ts / 1000.0, tz=timezone.utc)
        if last_ts is not None
        else at
    )
    return {
        "symbol": symbol,
        "as_of_requested": at,
        "as_of": book_ts,
        "segment": str(path),
        "events_applied": events,
        "u_gaps": gaps,
        "mid": mid,
        "best_bid": bb,
        "best_ask": ba,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "bids": bids,
        "asks": asks,
        "last_u": book.last_u,
        "genuine_200": len(bids) >= 180 and len(asks) >= 180,
    }


def replay_hour_at_cutoffs(
    ob_root: Path,
    symbol: str,
    hour_start: datetime,
    cutoffs: list[datetime],
) -> list[dict[str, Any]]:
    hour_start = (
        hour_start.astimezone(timezone.utc)
        if hour_start.tzinfo
        else hour_start.replace(tzinfo=timezone.utc)
    )
    hour_end = hour_start + timedelta(hours=1)
    path = find_hour_segment(ob_root, symbol, hour_start)
    ordered = sorted(
        {
            (c.astimezone(timezone.utc) if c.tzinfo else c.replace(tzinfo=timezone.utc))
            for c in cutoffs
            if hour_start
            <= (c.astimezone(timezone.utc) if c.tzinfo else c.replace(tzinfo=timezone.utc))
            < hour_end
        }
    )
    if not ordered:
        return []
    targets = [(int(c.timestamp() * 1000), c) for c in ordered]
    ti = 0
    book = MutableBook()
    last_ts = None
    events = 0
    gaps = 0
    out: list[dict[str, Any]] = []

    def emit(at: datetime) -> None:
        if not book.is_valid or not book.bids or not book.asks:
            out.append({"ts": at, "ok": False, "error": "invalid_book", "segment": str(path)})
            return
        bids = book.sorted_bids()
        asks = book.sorted_asks()
        bb, ba = bids[0][0], asks[0][0]
        if bb >= ba:
            out.append({"ts": at, "ok": False, "error": "crossed", "segment": str(path)})
            return
        mid = (bb + ba) / 2
        book_ts = (
            datetime.fromtimestamp(last_ts / 1000.0, tz=timezone.utc)
            if last_ts is not None
            else at
        )
        out.append(
            {
                "ok": True,
                "symbol": symbol,
                "as_of_requested": at,
                "as_of": book_ts,
                "segment": str(path),
                "events_applied": events,
                "u_gaps": gaps,
                "mid": mid,
                "best_bid": bb,
                "best_ask": ba,
                "bid_levels": len(bids),
                "ask_levels": len(asks),
                "bids": list(bids),
                "asks": list(asks),
                "last_u": book.last_u,
                "genuine_200": len(bids) >= 180 and len(asks) >= 180,
            }
        )

    for obj in iter_ndjson(path):
        ts = obj.get("ts")
        while ti < len(targets) and isinstance(ts, int) and ts >= targets[ti][0]:
            emit(targets[ti][1])
            ti += 1
        if ti >= len(targets):
            break
        typ = obj.get("type")
        data = obj.get("data") or {}
        if typ in ("snapshot", "rotation_checkpoint"):
            book.apply_snapshot(data)
        elif typ == "delta":
            prev_valid = book.is_valid
            prev_u = book.last_u
            book.apply_delta(data)
            if prev_valid and not book.is_valid and int(data.get("u") or 0) != prev_u:
                gaps += 1
        else:
            continue
        if isinstance(ts, int):
            last_ts = ts
        events += 1
    while ti < len(targets):
        emit(targets[ti][1])
        ti += 1
    return out


def extract_walls(book_snap: dict[str, Any], *, max_walls: int = 10) -> list[dict[str, Any]]:
    mid = book_snap["mid"]
    wall_max = Decimal(str(WALL_MAX_BPS))

    def side_walls(levels: list[tuple[Decimal, Decimal]], side: str) -> list[dict[str, Any]]:
        if not levels or mid <= ZERO:
            return []
        thr = mid * wall_max / Decimal("10000")
        in_range = [(p, q) for p, q in levels if abs(p - mid) <= thr] or levels
        qtys = [float(q) for _, q in in_range]
        med = statistics.median(qtys) if qtys else 0.0
        if med <= 0:
            return []
        walls = []
        for price, qty in in_range:
            ratio = float(qty) / med
            if ratio < WALL_QTY_MEDIAN_MULT:
                continue
            walls.append(
                {
                    "side": side,
                    "price": price,
                    "qty": qty,
                    "notional": price * qty,
                    "distance_bps": abs(price - mid) / mid * Decimal("10000"),
                    "ratio": ratio,
                }
            )
        walls.sort(key=lambda w: w["notional"], reverse=True)
        return walls[:max_walls]

    return side_walls(book_snap["bids"], "BID") + side_walls(book_snap["asks"], "ASK")


def audit_ob_coverage(
    ob_root: Path,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    needed = []
    t = window_start.replace(minute=0, second=0, microsecond=0)
    if t > window_start:
        t -= timedelta(hours=1)
    while t < window_end:
        try:
            path = find_hour_segment(ob_root, symbol, t)
            needed.append(
                {
                    "hour": t.isoformat().replace("+00:00", "Z"),
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "ok": path.stat().st_size > 10000,
                }
            )
        except Exception as exc:
            needed.append({"hour": t.isoformat().replace("+00:00", "Z"), "ok": False, "error": str(exc)})
        t += timedelta(hours=1)
    return {
        "source": str(ob_root / symbol),
        "engine": "filesystem ob200_v3.zst hour archives via research/btc_ob_fight/ob_replay.py",
        "hours_needed": needed,
        "all_hours_ok": all(h.get("ok") for h in needed),
        "depth": 200,
    }
