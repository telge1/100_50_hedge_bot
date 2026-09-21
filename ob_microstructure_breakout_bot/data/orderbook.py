from __future__ import annotations

from datetime import datetime
from glob import glob
from pathlib import Path

from ob_microstructure_breakout_bot.models import ObBandSnapshot

_DEFAULT_RAW_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/data/"
    "orderbook_raw_shadow/full_ob_v1"
)


def _band_notional(levels: dict, mid: float, side: str, bps: float) -> float:
    total = 0.0
    for p, q in levels.items():
        dist = ((mid - p) if side == "bid" else (p - mid)) / mid * 10000.0
        if 0 <= dist <= bps:
            total += p * q
    return total


def _resolve_segment(symbol: str, when: datetime, raw_root: Path) -> Path:
    day = when.strftime("%Y/%m/%d")
    hour = when.strftime("%Y%m%dT%H0000Z")
    pattern = str(raw_root / symbol / day / f"{symbol}_{hour}_*.ndjson.zst")
    files = sorted(glob(pattern))
    if not files:
        raise FileNotFoundError(f"No OB segment for {symbol} at {when.isoformat()}: {pattern}")
    return Path(files[0])


def sample_ob_bands(
    symbol: str,
    when: datetime,
    *,
    raw_root: Path | None = None,
) -> ObBandSnapshot:
    """Replay Full-OB archive until ``when`` and sample 5/10 bps bands.

    Strict exclusive upper bound: only records with ``event_time < when`` are
    applied. The first record with ``event_time >= when`` stops replay without
    being applied (no lookahead).
    """
    from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
    from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.replay import (
        iter_records,
    )

    root = raw_root or _DEFAULT_RAW_ROOT
    seg = _resolve_segment(symbol, when, root)
    state = FullBookState(symbol=symbol)
    # Compare in ns so sub-microsecond archive stamps keep a strict < when bound
    # (datetime only has microsecond resolution).
    when_ns = int(when.timestamp() * 1_000_000_000)

    def snapshot_from_state() -> ObBandSnapshot:
        snap = state.copy_consistent_snapshot()
        mid = snap.mid()
        if mid is None:
            raise RuntimeError(f"No mid at {when.isoformat()}")
        return ObBandSnapshot(
            bid_5bps=_band_notional(snap.bids, mid, "bid", 5.0),
            ask_5bps=_band_notional(snap.asks, mid, "ask", 5.0),
            bid_10bps=_band_notional(snap.bids, mid, "bid", 10.0),
            ask_10bps=_band_notional(snap.asks, mid, "ask", 10.0),
        )

    for rec in iter_records(seg):
        # Stop before applying any record at or after ``when``.
        if int(rec["event_time_ns"]) >= when_ns:
            return snapshot_from_state()

        payload = rec["original_payload"]
        kind = rec["message_type"]
        if kind == "checkpoint":
            state.apply_snapshot(
                bids=payload.get("bids") or [],
                asks=payload.get("asks") or [],
                u=payload.get("u"),
                seq=payload.get("seq"),
                ts_ms=int(payload["event_time"]) // 1_000_000
                if payload.get("event_time") is not None
                else None,
                receive_time_ns=payload.get("receive_time"),
                mark_ready=True,
            )
        elif kind == "delta":
            data = payload.get("data") or {}
            state.apply_delta(
                bids=data.get("b") or [],
                asks=data.get("a") or [],
                u=data.get("u"),
                seq=data.get("seq"),
                ts_ms=payload.get("ts") or data.get("ts"),
                cts_ms=payload.get("cts") or data.get("cts"),
                receive_time_ns=rec.get("receive_time_ns"),
                enforce_continuity=False,
            )
        else:
            continue

    raise RuntimeError(f"Target {when.isoformat()} not reached in {seg}")
