"""Live OB1000 sampler for dry-run (reads collector archive, not full_ob_v1)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_OB1000_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/data/"
    "orderbook_raw_shadow/ob1000_v1"
)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _band_notional(levels: dict, mid: float, side: str, bps: float) -> float:
    total = 0.0
    for price, qty in levels.items():
        dist = ((mid - price) if side == "bid" else (price - mid)) / mid * 10000.0
        if 0.0 <= dist <= bps:
            total += float(price) * float(qty)
    return total


def resolve_ob1000_segment(
    symbol: str,
    when: datetime,
    *,
    raw_root: Path | None = None,
) -> Path:
    """Pick the best OB1000 segment for ``when`` (open .tmp preferred)."""
    root = raw_root or _DEFAULT_OB1000_ROOT
    when = _ensure_utc(when)
    day = when.strftime("%Y/%m/%d")
    hour = when.strftime("%Y%m%dT%H0000Z")
    day_dir = root / symbol.upper() / day
    if not day_dir.is_dir():
        raise FileNotFoundError(f"No OB1000 day dir for {symbol} at {day_dir}")

    # Prefer the open live segment for this hour, then sealed hour files.
    preferred = sorted(day_dir.glob(f"{symbol.upper()}_{hour}_*"))
    if preferred:
        # open tmp last if present
        tmp = [p for p in preferred if p.name.endswith(".tmp")]
        if tmp:
            return sorted(tmp, key=lambda p: p.stat().st_mtime)[-1]
        return sorted(preferred, key=lambda p: p.stat().st_mtime)[-1]

    any_files = [p for p in day_dir.iterdir() if p.is_file()]
    if not any_files:
        raise FileNotFoundError(f"No OB1000 files for {symbol} in {day_dir}")
    return sorted(any_files, key=lambda p: p.stat().st_mtime)[-1]


def sample_ob1000_bands(
    symbol: str,
    when: datetime | None = None,
    *,
    raw_root: Path | None = None,
) -> tuple[float, float, float, float]:
    """Replay live OB1000 archive and return bid/ask notionals at 5/10 bps.

    Returns ``(bid_5bps, ask_5bps, bid_10bps, ask_10bps)``.
    """
    from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState

    try:
        import orjson
    except ImportError:  # pragma: no cover
        import json as orjson  # type: ignore

    try:
        import zstandard as zstd
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("zstandard required for OB1000 dry-run sampling") from exc

    as_of = _ensure_utc(when or datetime.now(timezone.utc))
    when_ms = int(as_of.timestamp() * 1000)
    path = resolve_ob1000_segment(symbol, as_of, raw_root=raw_root)
    state = FullBookState(symbol=symbol.upper())
    dctx = zstd.ZstdDecompressor()
    applied = 0

    with path.open("rb") as fh, dctx.stream_reader(fh) as reader:
        buf = b""
        while True:
            chunk = reader.read(1 << 20)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    obj = orjson.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                ts_ms = obj.get("ts")
                try:
                    ts_i = int(ts_ms) if ts_ms is not None else None
                except (TypeError, ValueError):
                    ts_i = None
                # Strict exclusive upper bound like the backtester sampler.
                if ts_i is not None and ts_i >= when_ms:
                    snap = state.copy_consistent_snapshot()
                    mid = snap.mid()
                    if mid is None:
                        raise RuntimeError(f"No mid for {symbol} at {as_of.isoformat()}")
                    return (
                        _band_notional(snap.bids, mid, "bid", 5.0),
                        _band_notional(snap.asks, mid, "ask", 5.0),
                        _band_notional(snap.bids, mid, "bid", 10.0),
                        _band_notional(snap.asks, mid, "ask", 10.0),
                    )

                kind = str(obj.get("type") or "")
                data = obj.get("data") or {}
                if kind in ("snapshot", "rotation_checkpoint"):
                    state.apply_snapshot(
                        bids=data.get("b") or [],
                        asks=data.get("a") or [],
                        u=data.get("u"),
                        seq=data.get("seq"),
                        ts_ms=ts_i,
                        mark_ready=True,
                    )
                    applied += 1
                elif kind == "delta":
                    state.apply_delta(
                        bids=data.get("b") or [],
                        asks=data.get("a") or [],
                        u=data.get("u"),
                        seq=data.get("seq"),
                        ts_ms=ts_i,
                        enforce_continuity=False,
                    )
                    applied += 1

    if applied <= 0:
        raise RuntimeError(f"No OB1000 book updates in {path}")
    snap = state.copy_consistent_snapshot()
    mid = snap.mid()
    if mid is None:
        raise RuntimeError(f"No mid for {symbol} after replay of {path}")
    return (
        _band_notional(snap.bids, mid, "bid", 5.0),
        _band_notional(snap.asks, mid, "ask", 5.0),
        _band_notional(snap.bids, mid, "bid", 10.0),
        _band_notional(snap.asks, mid, "ask", 10.0),
    )


def sample_ob1000_ratio(
    symbol: str,
    when: datetime | None = None,
    *,
    raw_root: Path | None = None,
) -> float | None:
    """Return bid_5bps / ask_5bps from live OB1000, or None on failure."""
    try:
        bid5, ask5, _b10, _a10 = sample_ob1000_bands(symbol, when, raw_root=raw_root)
        if ask5 <= 0:
            return None
        return float(bid5 / ask5)
    except Exception:
        return None
