"""Read-only data loading for wall toxicity audit."""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from orderbook_analyse.dynamic_wall_detector import ReadOnlyClickHouse, connect_readonly
from orderbook_analyse.wall_toxicity_audit.metrics import TradeTick
from orderbook_analyse.wall_toxicity_audit.types import WallSequenceRef

logger = logging.getLogger(__name__)


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def parse_utc(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    text = str(value).strip().replace("Z", "+00:00")
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    return ensure_utc(datetime.fromisoformat(text))


def open_readonly_db() -> ReadOnlyClickHouse:
    return connect_readonly()


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {"1", "true", "t", "yes"}


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


def load_wall_sequence_from_csv(
    path: Path, *, sequence_id: str
) -> WallSequenceRef:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        if str(row.get("wall_sequence_id") or "") == sequence_id:
            return wall_sequence_from_row(row)
    raise KeyError(f"sequence_id {sequence_id!r} not found in {path}")


def wall_sequence_from_row(row: dict[str, Any]) -> WallSequenceRef:
    return WallSequenceRef(
        symbol=str(row["symbol"]),
        segment_id=str(row.get("segment_id") or ""),
        wall_sequence_id=str(row["wall_sequence_id"]),
        side=str(row["side"]).lower(),
        resolution=str(row.get("resolution") or "auto_10bps"),
        first_seen_ts=parse_utc(row["first_seen_ts"])  # type: ignore[arg-type]
        or datetime(1970, 1, 1, tzinfo=timezone.utc),
        last_seen_ts=parse_utc(row.get("last_seen_ts"))  # type: ignore[arg-type]
        or parse_utc(row["first_seen_ts"])  # type: ignore[arg-type]
        or datetime(1970, 1, 1, tzinfo=timezone.utc),
        closed_ts=parse_utc(row.get("closed_ts")),
        first_price=float(row["first_price"]),
        last_price=float(row.get("last_price") or row["first_price"]),
        min_price=float(row.get("min_price") or row["first_price"]),
        max_price=float(row.get("max_price") or row["first_price"]),
        min_distance_bps=_as_float(row.get("min_distance_bps")),
        max_distance_bps=_as_float(row.get("max_distance_bps")),
        was_near_price=_as_bool(row.get("was_near_price")),
        was_tested=_as_bool(row.get("was_tested")),
        touched=_as_bool(row.get("touched")),
        disappeared_before_test=_as_bool(row.get("disappeared_before_test")),
        end_reason=str(row.get("end_reason") or ""),
        first_notional=_as_float(row.get("first_notional")),
        last_notional=_as_float(row.get("last_notional")),
        raw=dict(row),
    )


def load_wall_sequences_from_csv(
    path: Path,
    *,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    sequence_status: str | None = None,
) -> list[WallSequenceRef]:
    """Load and optionally filter wall sequences (read-only CSV)."""
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out: list[WallSequenceRef] = []
    for row in rows:
        seq = wall_sequence_from_row(row)
        if symbol and seq.symbol != symbol:
            continue
        if start is not None and seq.first_seen_ts < ensure_utc(start):
            continue
        if end is not None and seq.first_seen_ts > ensure_utc(end):
            continue
        if sequence_status:
            want = sequence_status.strip().upper()
            reason = (seq.end_reason or "").upper()
            if want == "OPEN" and reason not in {"", "ACTIVE", "OPEN"}:
                # keep sequences without closed end if requested
                if seq.closed_ts is not None:
                    continue
            elif want == "CLOSED" and seq.closed_ts is None:
                continue
            elif want not in {"OPEN", "CLOSED", "ALL", ""} and want not in reason:
                continue
        out.append(seq)
        if limit is not None and len(out) >= limit:
            break
    return out


def sequence_csv_time_span(path: Path) -> tuple[datetime | None, datetime | None]:
    seqs = load_wall_sequences_from_csv(path)
    if not seqs:
        return None, None
    first = min(s.first_seen_ts for s in seqs)
    lasts = [(s.closed_ts or s.last_seen_ts) for s in seqs]
    return first, max(lasts)


def load_price_series(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, float]]:
    """Preferred mid from bid/ask; fallback last_price. Sparse windows allowed."""
    return load_ticker_mids(db, symbol=symbol, start=start, end=end)


def default_wall_sequences_csv(symbol: str) -> Path | None:
    """Prefer newest-looking research artefacts that typically contain sequences."""
    root = Path(__file__).resolve().parents[3] / "results"
    candidates = [
        root / f"full_history_{symbol}_phase4" / "wall_sequences.csv",
        root / f"full_history_{symbol}_phase5" / "wall_sequences.csv",
        root / f"general_{symbol}" / "full_history" / "wall_sequences.csv",
        root / f"general_{symbol}_phase6_operator_20260727" / "full_history" / "wall_sequences.csv",
    ]
    for p in candidates:
        if p.exists():
            return p
    # glob fallback
    matches = sorted(root.glob(f"**/{symbol}*/**/wall_sequences.csv"))
    return matches[0] if matches else None


def load_level_updates(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    side: str,
    price_low: float,
    price_high: float,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Load absolute-quantity level rows (snapshot + delta) in price band."""
    side_l = "ask" if str(side).lower() in {"ask", "sell"} else "bid"
    rows = db.query(
        """
        SELECT
            exchange_ts, side, price, quantity, message_type,
            update_id, cross_sequence, level_index
        FROM orderbook_deltas
        WHERE symbol = %(symbol)s
          AND side = %(side)s
          AND price >= %(plo)s
          AND price <= %(phi)s
          AND exchange_ts >= %(start)s
          AND exchange_ts <= %(end)s
        ORDER BY exchange_ts, cross_sequence, update_id, level_index
        """,
        parameters={
            "symbol": symbol,
            "side": side_l,
            "plo": price_low,
            "phi": price_high,
            "start": ensure_utc(start),
            "end": ensure_utc(end),
        },
    ).result_rows
    out: list[dict[str, Any]] = []
    for r in rows:
        ts = r[0]
        ts = ensure_utc(ts) if getattr(ts, "tzinfo", None) else ts.replace(tzinfo=timezone.utc)
        out.append(
            {
                "exchange_ts": ts,
                "side": str(r[1]),
                "price": float(r[2]),
                "quantity": float(r[3]),
                "message_type": str(r[4]),
                "update_id": int(r[5]),
                "cross_sequence": int(r[6]),
                "level_index": int(r[7]) if r[7] is not None else 0,
            }
        )
    return out


def load_trades(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    price_low: float | None = None,
    price_high: float | None = None,
) -> list[TradeTick]:
    where = [
        "symbol = %(symbol)s",
        "trade_ts >= %(start)s",
        "trade_ts <= %(end)s",
    ]
    params: dict[str, Any] = {
        "symbol": symbol,
        "start": ensure_utc(start),
        "end": ensure_utc(end),
    }
    if price_low is not None:
        where.append("price >= %(plo)s")
        params["plo"] = price_low
    if price_high is not None:
        where.append("price <= %(phi)s")
        params["phi"] = price_high
    rows = db.query(
        f"""
        SELECT trade_ts, side, price, quantity, notional
        FROM public_trades
        WHERE {' AND '.join(where)}
        ORDER BY trade_ts, trade_id
        """,
        parameters=params,
    ).result_rows
    out: list[TradeTick] = []
    for ts, side, price, qty, notional in rows:
        ts = ensure_utc(ts) if getattr(ts, "tzinfo", None) else ts.replace(tzinfo=timezone.utc)
        px = float(price)
        q = float(qty or 0)
        n = float(notional) if notional is not None else px * q
        out.append(TradeTick(ts=ts, side=str(side), price=px, qty=q, notional=n))
    return out


def load_ticker_mids(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, float]]:
    rows = db.query(
        """
        SELECT exchange_ts, best_bid_price, best_ask_price, last_price
        FROM ticker_samples
        WHERE symbol = %(symbol)s
          AND exchange_ts >= %(start)s
          AND exchange_ts <= %(end)s
        ORDER BY exchange_ts
        """,
        parameters={
            "symbol": symbol,
            "start": ensure_utc(start),
            "end": ensure_utc(end),
        },
    ).result_rows
    out: list[tuple[datetime, float]] = []
    for ts, bb, ba, last in rows:
        ts = ensure_utc(ts) if getattr(ts, "tzinfo", None) else ts.replace(tzinfo=timezone.utc)
        mid = None
        if bb is not None and ba is not None and float(bb) > 0 and float(ba) > 0:
            mid = (float(bb) + float(ba)) / 2.0
        elif last is not None and float(last) > 0:
            mid = float(last)
        if mid is not None:
            out.append((ts, mid))
    return out


def load_best_quotes(
    db: ReadOnlyClickHouse,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, float | None, float | None]]:
    rows = db.query(
        """
        SELECT exchange_ts, best_bid_price, best_ask_price
        FROM ticker_samples
        WHERE symbol = %(symbol)s
          AND exchange_ts >= %(start)s
          AND exchange_ts <= %(end)s
        ORDER BY exchange_ts
        """,
        parameters={
            "symbol": symbol,
            "start": ensure_utc(start),
            "end": ensure_utc(end),
        },
    ).result_rows
    out: list[tuple[datetime, float | None, float | None]] = []
    for ts, bb, ba in rows:
        ts = ensure_utc(ts) if getattr(ts, "tzinfo", None) else ts.replace(tzinfo=timezone.utc)
        out.append(
            (
                ts,
                float(bb) if bb is not None else None,
                float(ba) if ba is not None else None,
            )
        )
    return out
