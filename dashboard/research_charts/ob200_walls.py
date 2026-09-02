"""Multi-level walls from local OB200 raw archive (shadow/live).

Self-contained replay (no orderbook_analyse import) so the dashboard venv
does not need orjson/zstandard.

Reads:
  - closed ``*_ob200_v3.zst`` hour segments
  - open writer files ``*_open_ob200_v3.zst.tmp`` for near-realtime
    (incomplete zstd frames are tolerated; last complete NDJSON lines win)

Roots: ``data/orderbook_raw_shadow/ob200_v3`` (+ optional live root).
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

ZERO = Decimal("0")
_NAME_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+)_"
    r"(?P<start>\d{8}T\d{6}Z)_"
    r"(?P<end>\d{8}T\d{6}Z)_"
    r"ob200_v3\.zst$"
)
_OPEN_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+)_"
    r"(?P<start>\d{8}T\d{6}Z)_"
    r"open_ob200_v3\.zst\.tmp$"
)

# Default: OA project next to this repo.
_OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
DEFAULT_SHADOW_ROOT = _OA_ROOT / "data" / "orderbook_raw_shadow" / "ob200_v3"
DEFAULT_LIVE_ROOT = _OA_ROOT / "data" / "orderbook_raw_live" / "ob200_v3"

WALL_MAX_BPS = Decimal("800")
WALL_QTY_MEDIAN_MULT = 3.0
MAX_WALLS_PER_SIDE = 60

_CACHE_MAX = 48
_CACHE_TTL = 45.0
_LIVE_CACHE_TTL = 8.0
_cache_lock = threading.Lock()
_book_cache: OrderedDict[tuple, tuple[float, dict[str, Any]]] = OrderedDict()


class Ob200WallsError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SegmentRef:
    path: Path
    symbol: str
    start_utc: datetime
    end_utc: datetime
    is_open: bool = False

    @property
    def is_boundary_stub(self) -> bool:
        return (not self.is_open) and self.start_utc == self.end_utc


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


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _parse_stamp(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _zstd_open(path: Path):
    """Return a binary file-like reader over decompressed NDJSON."""
    try:
        import zstandard as zstd  # type: ignore

        if hasattr(zstd.ZstdDecompressor(), "stream_reader"):
            return zstd.ZstdDecompressor().stream_reader(path.open("rb"))
    except ImportError:
        pass

    try:
        import backports.zstd as bz  # type: ignore

        return bz.open(path, "rb")
    except ImportError as exc:
        raise Ob200WallsError(
            "zstd_missing", "zstandard / backports.zstd not installed"
        ) from exc


def iter_decompressed_objects(path: Path) -> Iterator[dict[str, Any]]:
    """Yield complete NDJSON objects; tolerate truncated open zstd streams.

    Always use chunked ``read()``. Some zstd stream readers expose ``readline()``
    but raise ``io.UnsupportedOperation`` (e.g. ``ZstdDecompressionReader``),
    which previously aborted iteration with zero events.
    """
    reader = _zstd_open(path)
    try:
        buf = b""
        while True:
            try:
                chunk = reader.read(1 << 20)
            except (EOFError, OSError, ValueError):
                # Open .tmp: frame incomplete until writer flushes/closes.
                break
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
                    # Partial trailing line while writer is mid-flush.
                    continue
                if isinstance(obj, dict):
                    yield obj
        # Do not parse leftover buf — may be incomplete on open streams.
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _is_replayable(obj: dict[str, Any]) -> bool:
    if "archive_event" in obj:
        return False
    return obj.get("type") in ("snapshot", "delta", "rotation_checkpoint")


def _replay_payload(obj: dict[str, Any]) -> dict[str, Any]:
    if obj.get("type") == "rotation_checkpoint":
        return {"type": "snapshot", "data": obj.get("data") or {}, "ts": obj.get("ts")}
    return {"type": obj.get("type"), "data": obj.get("data") or {}, "ts": obj.get("ts")}


def list_closed_segments(
    roots: list[Path],
    symbol: str,
    *,
    include_boundary_stubs: bool = False,
) -> list[SegmentRef]:
    sym = symbol.upper()
    out: list[SegmentRef] = []
    seen: set[Path] = set()
    for root in roots:
        sym_root = root / sym
        if not sym_root.is_dir():
            continue
        for path in sorted(sym_root.rglob("*_ob200_v3.zst")):
            if path in seen:
                continue
            name = path.name
            if "_open_" in name or name.endswith(".tmp"):
                continue
            m = _NAME_RE.match(name)
            if not m:
                continue
            ref = SegmentRef(
                path=path,
                symbol=m.group("symbol"),
                start_utc=_parse_stamp(m.group("start")),
                end_utc=_parse_stamp(m.group("end")),
                is_open=False,
            )
            if not include_boundary_stubs and ref.is_boundary_stub:
                continue
            seen.add(path)
            out.append(ref)
    out.sort(key=lambda r: (r.start_utc, r.end_utc, str(r.path)))
    return out


def list_open_segments(roots: list[Path], symbol: str) -> list[SegmentRef]:
    """Currently-writing hour files (``*_open_ob200_v3.zst.tmp``)."""
    sym = symbol.upper()
    out: list[SegmentRef] = []
    seen: set[Path] = set()
    now = datetime.now(timezone.utc)
    for root in roots:
        sym_root = root / sym
        if not sym_root.is_dir():
            continue
        for path in sorted(sym_root.rglob("*_open_ob200_v3.zst.tmp")):
            if path in seen or not path.is_file():
                continue
            m = _OPEN_RE.match(path.name)
            if not m or m.group("symbol") != sym:
                continue
            start = _parse_stamp(m.group("start"))
            end = start + timedelta(hours=1)
            if end < now:
                end = now
            seen.add(path)
            out.append(
                SegmentRef(
                    path=path,
                    symbol=sym,
                    start_utc=start,
                    end_utc=end,
                    is_open=True,
                )
            )
    out.sort(key=lambda r: (r.start_utc, str(r.path)))
    return out


def coverage_bounds(symbol: str, *, roots: list[Path] | None = None) -> tuple[datetime, datetime] | None:
    roots = roots or [DEFAULT_SHADOW_ROOT, DEFAULT_LIVE_ROOT]
    segs = list_closed_segments(roots, symbol)
    opens = list_open_segments(roots, symbol)
    if not segs and not opens:
        return None
    starts = [s.start_utc for s in segs] + [s.start_utc for s in opens]
    ends = [s.end_utc for s in segs] + [s.end_utc for s in opens]
    return min(starts), max(ends)


def has_ob200_archive(symbol: str, *, roots: list[Path] | None = None) -> bool:
    return coverage_bounds(symbol, roots=roots) is not None


def _pick_segment(segs: list[SegmentRef], at: datetime) -> SegmentRef | None:
    """Segment covering ``at``, else last segment ending at/before ``at``, else None."""
    at_u = _utc(at)
    covering = [s for s in segs if s.start_utc <= at_u < s.end_utc]
    if covering:
        return covering[-1]
    ending = [s for s in segs if s.end_utc == at_u]
    if ending:
        return ending[-1]
    before = [s for s in segs if s.end_utc <= at_u]
    if before:
        return before[-1]
    return None


def _pick_replay_target(
    closed: list[SegmentRef],
    opens: list[SegmentRef],
    at: datetime,
) -> tuple[SegmentRef, datetime, bool]:
    """Return (segment, effective_at, clamped). Prefer open file for live tip."""
    at_u = _utc(at)
    if not closed and not opens:
        raise Ob200WallsError("ob200_missing", "no OB200 archive segments")

    cov_start = min([s.start_utc for s in closed] + [s.start_utc for s in opens])
    if at_u < cov_start:
        raise Ob200WallsError(
            "ob200_before_coverage",
            f"as-of {at_u.isoformat()} before OB200 coverage {cov_start.isoformat()}",
        )

    closed_end = closed[-1].end_utc if closed else None
    live_opens = [s for s in opens if s.start_utc <= at_u]
    if live_opens and (closed_end is None or at_u >= closed_end or at_u >= live_opens[-1].start_utc):
        open_ref = live_opens[-1]
        if at_u >= open_ref.start_utc:
            return open_ref, at_u, False

    if not closed:
        open_ref = live_opens[-1] if live_opens else opens[-1]
        return open_ref, max(at_u, open_ref.start_utc), False

    effective = at_u
    clamped = False
    if closed_end is not None and effective > closed_end and not live_opens:
        effective = closed_end
        clamped = True
    ref = _pick_segment(closed, effective)
    if ref is None:
        raise Ob200WallsError("ob200_no_segment", "no segment for as-of time")
    return ref, effective, clamped


def _extract_walls(
    levels: list[tuple[Decimal, Decimal]],
    mid: Decimal,
    *,
    side: str,
    max_bps: Decimal = WALL_MAX_BPS,
    qty_median_mult: float = WALL_QTY_MEDIAN_MULT,
    max_walls: int = MAX_WALLS_PER_SIDE,
) -> list[dict[str, Any]]:
    if not levels or mid <= ZERO:
        return []
    thr = mid * max_bps / Decimal("10000")
    in_range = [(p, q) for p, q in levels if abs(p - mid) <= thr]
    if not in_range:
        in_range = levels
    qtys = [float(q) for _, q in in_range]
    if not qtys:
        return []
    med = statistics.median(qtys)
    if med <= 0:
        return []
    walls: list[dict[str, Any]] = []
    for price, qty in in_range:
        ratio = float(qty) / med
        if ratio < qty_median_mult:
            continue
        notional = price * qty
        dist_bps = float(abs(price - mid) / mid * Decimal("10000"))
        walls.append(
            {
                "side": side,
                "price": price,
                "qty": qty,
                "notional": notional,
                "distance_bps": Decimal(str(dist_bps)),
                "ratio": ratio,
            }
        )
    walls.sort(key=lambda w: w["notional"], reverse=True)
    return walls[:max_walls]


def _replay_path(
    ref: SegmentRef,
    *,
    cutoff_ms: int | None,
) -> tuple[MutableBook, int | None, int]:
    book = MutableBook()
    last_ts: int | None = None
    events = 0
    for obj in iter_decompressed_objects(ref.path):
        ts = obj.get("ts")
        if cutoff_ms is not None and isinstance(ts, int) and ts >= cutoff_ms:
            break
        if not _is_replayable(obj):
            continue
        payload = _replay_payload(obj)
        data = payload.get("data") or {}
        mtype = payload.get("type")
        if mtype == "snapshot":
            book.apply_snapshot(data)
        elif mtype == "delta":
            book.apply_delta(data)
        if isinstance(ts, int):
            last_ts = ts
        events += 1
    return book, last_ts, events


def replay_book_as_of(
    symbol: str,
    at: datetime,
    *,
    roots: list[Path] | None = None,
) -> dict[str, Any]:
    """Replay closed or open segment to ``at`` (open = near-realtime)."""
    roots = roots or [DEFAULT_SHADOW_ROOT, DEFAULT_LIVE_ROOT]
    closed = list_closed_segments(roots, symbol)
    opens = list_open_segments(roots, symbol)
    if not closed and not opens:
        raise Ob200WallsError("ob200_missing", f"no OB200 archive for {symbol}")

    at_u = _utc(at)
    ref, effective, clamped = _pick_replay_target(closed, opens, at_u)
    cov = coverage_bounds(symbol, roots=roots)
    assert cov is not None
    cov_start, cov_end = cov

    at_ms = int(effective.timestamp() * 1000)
    if ref.is_open:
        cutoff_ms = at_ms + 1
    else:
        end_ms = int(ref.end_utc.timestamp() * 1000)
        cutoff_ms = at_ms if at_ms < end_ms else end_ms + 1

    book, last_ts, events = _replay_path(ref, cutoff_ms=cutoff_ms)

    if not book.is_valid or not book.bids or not book.asks:
        raise Ob200WallsError("ob200_invalid_book", "reconstructed book invalid or empty")

    bids = book.sorted_bids()
    asks = book.sorted_asks()
    bb, ba = bids[0][0], asks[0][0]
    if bb >= ba:
        raise Ob200WallsError("ob200_crossed_book", "reconstructed book is crossed")
    mid = (bb + ba) / 2
    book_ts = (
        datetime.fromtimestamp(last_ts / 1000.0, tz=timezone.utc)
        if last_ts is not None
        else effective
    )
    lag_s = max(0.0, (datetime.now(timezone.utc) - book_ts).total_seconds())
    return {
        "symbol": symbol.upper(),
        "as_of_requested": at_u,
        "as_of": book_ts,
        "clamped": clamped,
        "live_open": bool(ref.is_open),
        "lag_seconds": round(lag_s, 1),
        "coverage_start": cov_start,
        "coverage_end": cov_end,
        "segment": str(ref.path),
        "events_applied": events,
        "mid": mid,
        "best_bid": bb,
        "best_ask": ba,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "bids": bids,
        "asks": asks,
    }


def walls_from_book(
    book_snap: dict[str, Any],
    *,
    max_bps: Decimal = WALL_MAX_BPS,
    qty_median_mult: float = WALL_QTY_MEDIAN_MULT,
    max_walls: int = MAX_WALLS_PER_SIDE,
) -> list[dict[str, Any]]:
    mid = book_snap["mid"]
    bid_walls = _extract_walls(
        book_snap["bids"],
        mid,
        side="BID",
        max_bps=max_bps,
        qty_median_mult=qty_median_mult,
        max_walls=max_walls,
    )
    ask_walls = _extract_walls(
        book_snap["asks"],
        mid,
        side="ASK",
        max_bps=max_bps,
        qty_median_mult=qty_median_mult,
        max_walls=max_walls,
    )
    return bid_walls + ask_walls


def load_ob200_walls(
    *,
    symbol: str,
    at: datetime,
    roots: list[Path] | None = None,
    max_walls_per_side: int = MAX_WALLS_PER_SIDE,
) -> dict[str, Any]:
    """Cached multi-wall snapshot for Research Charts."""
    sym = str(symbol or "").strip().upper()
    at_u = _utc(at)
    now_utc = datetime.now(timezone.utc)
    live = abs((now_utc - at_u).total_seconds()) < 180
    bucket = 15 if live else 60
    ttl = _LIVE_CACHE_TTL if live else _CACHE_TTL
    open_sig = 0
    if live:
        for o in list_open_segments(roots or [DEFAULT_SHADOW_ROOT, DEFAULT_LIVE_ROOT], sym):
            try:
                open_sig = max(open_sig, int(o.path.stat().st_size))
            except OSError:
                pass
    cache_key = (sym, int(at_u.timestamp()) // bucket, max_walls_per_side, open_sig // 65536)
    now = time.monotonic()
    with _cache_lock:
        hit = _book_cache.get(cache_key)
        if hit and now < hit[0]:
            _book_cache.move_to_end(cache_key)
            out = dict(hit[1])
            out["cached"] = True
            return out

    snap = replay_book_as_of(sym, at_u, roots=roots)
    walls = walls_from_book(snap, max_walls=max_walls_per_side)
    payload = {
        "symbol": sym,
        "as_of": snap["as_of"],
        "as_of_requested": snap["as_of_requested"],
        "clamped": snap["clamped"],
        "live_open": snap.get("live_open", False),
        "lag_seconds": snap.get("lag_seconds"),
        "coverage_start": snap["coverage_start"],
        "coverage_end": snap["coverage_end"],
        "segment": snap["segment"],
        "events_applied": snap["events_applied"],
        "mid": snap["mid"],
        "best_bid": snap["best_bid"],
        "best_ask": snap["best_ask"],
        "bid_levels": snap["bid_levels"],
        "ask_levels": snap["ask_levels"],
        "walls": walls,
        "cached": False,
    }
    with _cache_lock:
        _book_cache[cache_key] = (now + ttl, dict(payload))
        _book_cache.move_to_end(cache_key)
        while len(_book_cache) > _CACHE_MAX:
            _book_cache.popitem(last=False)
    return payload


def clear_ob200_walls_cache_for_tests() -> None:
    with _cache_lock:
        _book_cache.clear()
