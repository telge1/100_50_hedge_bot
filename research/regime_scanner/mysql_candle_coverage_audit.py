"""Pure helpers + read-only MySQL candle coverage audit logic.

Does not mutate scanner rules or write to the database.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

# Extended map for audit only (DB may store TFs beyond scanner SUPPORTED_TIMEFRAMES).
AUDIT_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
}

SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "APTUSDT": ("APTUSDT", "APT/USDT", "APT/USDT:USDT", "APT_USDT", "APT_USDT_USDT"),
    "DOGEUSDT": ("DOGEUSDT", "DOGE/USDT", "DOGE/USDT:USDT", "DOGE_USDT", "DOGE_USDT_USDT"),
    "BTCUSDT": ("BTCUSDT", "BTC/USDT", "BTC/USDT:USDT", "BTC_USDT", "BTC_USDT_USDT"),
}


def timeframe_to_seconds(timeframe: str) -> int:
    key = str(timeframe).strip().lower()
    if key in AUDIT_TIMEFRAME_SECONDS:
        return AUDIT_TIMEFRAME_SECONDS[key]
    m = re.fullmatch(r"(\d+)([smhd])", key)
    if not m:
        raise ValueError(f"unsupported timeframe for audit: {timeframe!r}")
    n, unit = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return n * mult


def ensure_utc(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def candle_close_from_open(open_time: object, timeframe: str) -> pd.Timestamp:
    return ensure_utc(open_time) + pd.Timedelta(seconds=timeframe_to_seconds(timeframe))


def expected_row_count(first_open: object, last_open: object, timeframe: str) -> int:
    """Inclusive expected count of regularly spaced opens from first to last."""
    a = ensure_utc(first_open)
    b = ensure_utc(last_open)
    if b < a:
        return 0
    step = pd.Timedelta(seconds=timeframe_to_seconds(timeframe))
    if step <= pd.Timedelta(0):
        raise ValueError("non-positive timeframe step")
    n = int((b - a) / step) + 1
    return max(n, 0)


def find_gaps(open_times: Sequence[pd.Timestamp], timeframe: str) -> list[dict[str, Any]]:
    """Return gaps where successive opens differ by more than one interval."""
    if len(open_times) < 2:
        return []
    step = pd.Timedelta(seconds=timeframe_to_seconds(timeframe))
    gaps: list[dict[str, Any]] = []
    for i in range(1, len(open_times)):
        prev = ensure_utc(open_times[i - 1])
        cur = ensure_utc(open_times[i])
        delta = cur - prev
        if delta > step:
            missing = int(delta / step) - 1
            gaps.append(
                {
                    "prev_open_utc": prev.isoformat(),
                    "next_open_utc": cur.isoformat(),
                    "gap_seconds": float(delta.total_seconds()),
                    "missing_intervals": max(missing, 0),
                }
            )
    return gaps


def find_duplicate_opens(open_times: Sequence[pd.Timestamp]) -> int:
    s = pd.Series([ensure_utc(t) for t in open_times])
    return int(s.duplicated().sum())


def invalid_ohlcv_mask(df: pd.DataFrame) -> pd.Series:
    """Boolean mask of invalid OHLCV rows (True = invalid)."""
    o = pd.to_numeric(df["open"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    v = pd.to_numeric(df["volume"], errors="coerce") if "volume" in df.columns else pd.Series(0.0, index=df.index)
    nulls = o.isna() | h.isna() | low.isna() | c.isna() | v.isna()
    bad = (
        nulls
        | (h < low)
        | (h < o)
        | (h < c)
        | (low > o)
        | (low > c)
        | (o <= 0)
        | (h <= 0)
        | (low <= 0)
        | (c <= 0)
        | (v < 0)
    )
    return bad.fillna(True)


def select_last_closed_candle(
    opens: Sequence[object],
    closes: Sequence[object],
    query_timestamp: object,
) -> dict[str, Any]:
    """Causal selection: last candle with close_time <= query_timestamp."""
    q = ensure_utc(query_timestamp)
    best_i = None
    for i, (o, cl) in enumerate(zip(opens, closes)):
        close = ensure_utc(cl)
        if close <= q:
            best_i = i
        else:
            break  # assumes ascending open/close
    if best_i is None:
        # unsorted fallback
        for i, (o, cl) in enumerate(zip(opens, closes)):
            if ensure_utc(cl) <= q:
                if best_i is None or ensure_utc(closes[i]) > ensure_utc(closes[best_i]):
                    best_i = i
    if best_i is None:
        return {
            "requested_timestamp_utc": q.isoformat(),
            "selected_last_candle_open_utc": None,
            "selected_last_candle_close_utc": None,
            "causality_pass": True,
            "note": "no_closed_candle_at_or_before_query",
        }
    open_u = ensure_utc(opens[best_i])
    close_u = ensure_utc(closes[best_i])
    # causality: selected close <= query; next open must not be used if its close > query
    pass_ok = close_u <= q
    if best_i + 1 < len(closes):
        next_close = ensure_utc(closes[best_i + 1])
        # running candle must not be selected
        if next_close > q and open_u < q < next_close:
            # selected must still be previous
            pass_ok = pass_ok and close_u <= q
    return {
        "requested_timestamp_utc": q.isoformat(),
        "selected_last_candle_open_utc": open_u.isoformat(),
        "selected_last_candle_close_utc": close_u.isoformat(),
        "causality_pass": bool(pass_ok and close_u <= q),
        "note": "ok",
    }


def coverage_pct(row_count: int, expected: int) -> float | None:
    if expected <= 0:
        return None
    return float(row_count) / float(expected) * 100.0


def warmup_available(
    first_open: object,
    last_close: object,
    *,
    days: int,
) -> bool:
    """True if span from first open to last close covers at least ``days`` days."""
    a = ensure_utc(first_open)
    b = ensure_utc(last_close)
    return (b - a) >= pd.Timedelta(days=days)


def normalize_symbol_lookup(wanted: str, available: Iterable[str]) -> str | None:
    """Map APTUSDT-style request onto DB symbol if an alias matches."""
    avail = {str(s): str(s) for s in available}
    upper = {str(s).upper(): str(s) for s in available}
    w = str(wanted).strip()
    if w in avail:
        return avail[w]
    if w.upper() in upper:
        return upper[w.upper()]
    aliases = SYMBOL_ALIASES.get(w.upper(), (w.upper(),))
    for a in aliases:
        if a in avail:
            return avail[a]
        if a.upper() in upper:
            return upper[a.upper()]
    # soft match: strip separators
    def compact(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", s).upper()

    wc = compact(w)
    for s in available:
        if compact(s) == wc or compact(s).startswith(wc) or wc.startswith(compact(s)):
            return str(s)
    return None


@dataclass
class SeriesCoverage:
    exchange: str
    market_type: str | None
    symbol: str
    timeframe: str
    first_candle_open_utc: str | None
    first_candle_close_utc: str | None
    last_candle_open_utc: str | None
    last_candle_close_utc: str | None
    row_count: int
    distinct_days: int
    expected_row_count: int | None
    coverage_pct: float | None
    missing_intervals: int
    largest_gap_seconds: float | None
    duplicate_count: int
    invalid_ohlcv_count: int
    open_is_closed_mismatch: int
    null_row_count: int
    sources: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze_ohlcv_series(
    df: pd.DataFrame,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    market_type: str | None = None,
) -> SeriesCoverage:
    if df.empty:
        return SeriesCoverage(
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            timeframe=timeframe,
            first_candle_open_utc=None,
            first_candle_close_utc=None,
            last_candle_open_utc=None,
            last_candle_close_utc=None,
            row_count=0,
            distinct_days=0,
            expected_row_count=0,
            coverage_pct=None,
            missing_intervals=0,
            largest_gap_seconds=None,
            duplicate_count=0,
            invalid_ohlcv_count=0,
            open_is_closed_mismatch=0,
            null_row_count=0,
            sources={},
        )

    open_col = "open_time" if "open_time" in df.columns else "timestamp"
    opens = [ensure_utc(t) for t in df[open_col].tolist()]
    if "close_time" in df.columns:
        closes = [ensure_utc(t) for t in df["close_time"].tolist()]
    else:
        closes = [candle_close_from_open(o, timeframe) for o in opens]

    # sort by open
    order = np.argsort(opens)
    opens = [opens[i] for i in order]
    closes = [closes[i] for i in order]
    df_sorted = df.iloc[list(order)].reset_index(drop=True)

    gaps = find_gaps(opens, timeframe)
    missing = int(sum(g["missing_intervals"] for g in gaps))
    largest = max((g["gap_seconds"] for g in gaps), default=None)
    dups = find_duplicate_opens(opens)
    inv = int(invalid_ohlcv_mask(df_sorted).sum())
    nulls = int(df_sorted[["open", "high", "low", "close"]].isna().any(axis=1).sum())

    mismatch = 0
    step = pd.Timedelta(seconds=timeframe_to_seconds(timeframe))
    for o, c in zip(opens, closes):
        if abs((c - o) - step) > pd.Timedelta(milliseconds=1):
            mismatch += 1

    first_o, last_o = opens[0], opens[-1]
    first_c, last_c = closes[0], closes[-1]
    exp = expected_row_count(first_o, last_o, timeframe)
    days = int(pd.Series(opens).dt.floor("D").nunique())
    sources: dict[str, int] = {}
    if "source" in df_sorted.columns:
        sources = {str(k): int(v) for k, v in df_sorted["source"].value_counts().items()}

    return SeriesCoverage(
        exchange=exchange,
        market_type=market_type,
        symbol=symbol,
        timeframe=timeframe,
        first_candle_open_utc=first_o.isoformat(),
        first_candle_close_utc=first_c.isoformat(),
        last_candle_open_utc=last_o.isoformat(),
        last_candle_close_utc=last_c.isoformat(),
        row_count=int(len(df_sorted)),
        distinct_days=days,
        expected_row_count=exp,
        coverage_pct=coverage_pct(len(df_sorted) - dups, exp),
        missing_intervals=missing,
        largest_gap_seconds=largest,
        duplicate_count=dups,
        invalid_ohlcv_count=inv,
        open_is_closed_mismatch=mismatch,
        null_row_count=nulls,
        sources=sources,
    )
