"""Historical point-in-time 5m trend direction from MySQL + C3.4B structure.

Read-only. Does not mutate scanner rules. Primary output: BULLISH | BEARISH | UNCLEAR.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from research.regime_scanner.market_structure_c3_4b import (
    RESEARCH_MATRIX,
    ProtectedStructureConfig,
    apply_protected_structure,
)
from research.regime_scanner.pullback_entry_c3_5 import enrich_indicators
from research.regime_scanner.timeframes import aggregate_candles, ensure_utc_timestamp

# Same operational floor used by trend_scanner_multitimeframe for in_warmup.
DEFAULT_WARMUP_BARS = 72
SOURCE_TIMEFRAME = "5m"
TF_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


class TrendDirectionAtError(Exception):
    """User-facing / CLI error with stable reason code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class TrendDirectionResult:
    symbol: str
    exchange: str
    requested_at_utc: str
    timestamp_assumed_utc: bool
    last_available_5m_open_utc: str | None
    last_available_5m_close_utc: str | None
    direction: str
    direction_since_utc: str | None
    source_timeframe: str
    structure_event: str | None
    causality_pass: bool
    reason: str | None
    warmup_bars_required: int
    warmup_bars_available: int
    protected_high: float | None = None
    protected_low: float | None = None
    major_direction: int | None = None
    protected_structure_state: str | None = None
    scanner_variant: str | None = None
    htf_context: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    s = s.replace("-", "_").replace(" ", "")
    if "/" in s:
        # APT/USDT:USDT → APTUSDT
        base = s.split(":")[0]
        s = base.replace("/", "")
    s = s.replace("_", "")
    if s.endswith("USDTUSDT"):
        s = s[: -len("USDT")] + "USDT"
    return s


def parse_decision_timestamp(value: object) -> tuple[pd.Timestamp, bool]:
    """Parse ISO timestamp; naive values are treated as UTC (assumed_utc=True)."""
    raw = str(value).strip()
    assumed = False
    # Explicit Z
    if raw.endswith("Z") or raw.endswith("z"):
        ts = pd.Timestamp(raw)
        return ensure_utc_timestamp(ts), False
    # Has offset
    if re.search(r"[+-]\d{2}:\d{2}$", raw) or re.search(r"[+-]\d{4}$", raw):
        return ensure_utc_timestamp(pd.Timestamp(raw)), False
    # Naive → UTC
    assumed = True
    ts = pd.Timestamp(raw)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts, assumed


def _iso_z(ts: object | None) -> str | None:
    if ts is None:
        return None
    t = ensure_utc_timestamp(ts)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def map_major_to_direction(major: object) -> str:
    try:
        m = int(major)
    except (TypeError, ValueError):
        return "UNCLEAR"
    if m == 1:
        return "BULLISH"
    if m == -1:
        return "BEARISH"
    return "UNCLEAR"


# States that mean the sticky major is challenged / in transition (not yet flipped).
# C3.4B keeps major_direction sticky until CHOCH hold; these states already exist on the bar.
_MAJOR_STATE_CONFLICT: frozenset[tuple[int, str]] = frozenset(
    {
        (1, "bearish_internal_break"),
        (1, "bearish_choch"),
        (1, "bearish_structure_candidate"),
        (1, "bearish_retest_pending"),
        (-1, "bullish_internal_break"),
        (-1, "bullish_choch"),
        (-1, "bullish_structure_candidate"),
        (-1, "bullish_retest_pending"),
    }
)
_NEUTRAL_STATES: frozenset[str] = frozenset(
    {"structure_unknown", "range_unclear", "transition_blocked"}
)


def map_structure_to_direction(major: object, protected_structure_state: object) -> str:
    """Map sticky major + current protected state to BULLISH/BEARISH/UNCLEAR.

    Uses existing C3.4B states only:
    - confirmed major with aligned state → BULLISH/BEARISH
    - major challenged by opposite internal break / CHOCH-pending → UNCLEAR
    - unknown/blocked → UNCLEAR
    Does not invent new thresholds; does not wait for opposite major flip.
    """
    try:
        m = int(major)
    except (TypeError, ValueError):
        m = 0
    state = str(protected_structure_state or "").strip()
    if m == 0 or state in _NEUTRAL_STATES or not state:
        return "UNCLEAR"
    if (m, state) in _MAJOR_STATE_CONFLICT:
        return "UNCLEAR"
    return map_major_to_direction(m)


def _structure_event_for_row(row: pd.Series, direction: str) -> str | None:
    cs = str(row.get("choch_side") or "").strip().lower()
    if direction == "BULLISH":
        if cs == "up":
            return "bullish_choch"
        if bool(row.get("external_bos_up")):
            return "bullish_bos"
        return "bullish_structure"
    if direction == "BEARISH":
        if cs == "down":
            return "bearish_choch"
        if bool(row.get("external_bos_down")):
            return "bearish_bos"
        return "bearish_structure"
    return None


def _bar_direction(row: pd.Series) -> str:
    major = int(row["major_direction"]) if pd.notna(row.get("major_direction")) else 0
    state = str(row.get("protected_structure_state") or "")
    return map_structure_to_direction(major, state)


def _direction_since_and_event(
    structure: pd.DataFrame,
    *,
    direction: str,
    major: int,
) -> tuple[str | None, str | None]:
    if structure.empty:
        return None, None
    if direction == "UNCLEAR":
        # Start of the continuous UNCLEAR run (not the prior sticky major).
        start_i = len(structure) - 1
        for i in range(len(structure) - 1, -1, -1):
            if _bar_direction(structure.iloc[i]) != "UNCLEAR":
                break
            start_i = i
        row = structure.iloc[start_i]
        open_ts = ensure_utc_timestamp(row["timestamp"])
        since = _iso_z(open_ts + pd.Timedelta(minutes=5))
        last_state = str(structure.iloc[-1].get("protected_structure_state") or "") or None
        return since, last_state

    majors = structure["major_direction"].fillna(0).astype(int)
    # start of current major-aligned directional run
    start_i = len(structure) - 1
    for i in range(len(structure) - 1, -1, -1):
        if int(majors.iloc[i]) != major:
            break
        if _bar_direction(structure.iloc[i]) != direction:
            break
        start_i = i
    row = structure.iloc[start_i]
    open_ts = ensure_utc_timestamp(row["timestamp"])
    since = _iso_z(open_ts + pd.Timedelta(minutes=5))  # available at close of that bar
    event = _structure_event_for_row(row, direction)
    # If flip bar has no flags, scan forward a few bars within the run for CHOCH/BOS
    if event in {"bullish_structure", "bearish_structure"}:
        end = min(len(structure), start_i + 6)
        for j in range(start_i, end):
            ev = _structure_event_for_row(structure.iloc[j], direction)
            if ev and (ev.endswith("_choch") or ev.endswith("_bos")):
                event = ev
                break
    return since, event


def reason_for_direction(*, direction: str, major: int, state: str, n: int, warmup_bars: int) -> str | None:
    if direction == "UNCLEAR":
        if n < warmup_bars:
            return "INSUFFICIENT_WARMUP"
        if state in _NEUTRAL_STATES or major == 0 or not state:
            return "STRUCTURE_UNKNOWN"
        if (major, state) in _MAJOR_STATE_CONFLICT:
            return f"MAJOR_CHALLENGED:{state}"
        return "STRUCTURE_UNKNOWN"
    return "MAJOR_CONFIRMED"


def run_c34b_on_ohlcv(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Apply protected_medium C3.4B to closed OHLCV (timestamp = candle open)."""
    need = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in ohlcv.columns]
    if missing:
        raise TrendDirectionAtError("INVALID_OHLCV", f"ohlcv missing columns: {missing}")
    frame = ohlcv[need].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    feat = enrich_indicators(frame)
    cfg = ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    struct = apply_protected_structure(feat, cfg)
    struct = struct.copy()
    struct["candle_open_ts"] = pd.to_datetime(struct["timestamp"], utc=True)
    struct["candle_close_ts"] = struct["candle_open_ts"] + pd.Timedelta(minutes=5)
    struct["available_at"] = struct["candle_close_ts"]
    return struct


def _htf_direction_from_5m(candles_5m: pd.DataFrame, decision: pd.Timestamp, tf: str) -> str:
    """Causal HTF diagnostic: aggregate complete buckets then C3.4B major_direction."""
    if tf not in ("15m", "30m"):
        return "UNCLEAR"
    try:
        htf = aggregate_candles(candles_5m, tf, decision)
    except Exception:  # noqa: BLE001
        return "UNCLEAR"
    if htf is None or htf.empty or len(htf) < 20:
        return "UNCLEAR"
    feat = enrich_indicators(htf)
    cfg = ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    struct = apply_protected_structure(feat, cfg)
    if struct.empty:
        return "UNCLEAR"
    return map_major_to_direction(struct["major_direction"].iloc[-1])


def decide_from_structure(
    structure: pd.DataFrame,
    *,
    decision_time: pd.Timestamp,
    symbol: str,
    exchange: str,
    timestamp_assumed_utc: bool,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    candles_5m: pd.DataFrame | None = None,
    include_htf: bool = False,
) -> TrendDirectionResult:
    """Map last causal structure row to direction result."""
    decision = ensure_utc_timestamp(decision_time)
    n = int(len(structure))
    if n == 0:
        return TrendDirectionResult(
            symbol=symbol,
            exchange=exchange,
            requested_at_utc=_iso_z(decision) or "",
            timestamp_assumed_utc=timestamp_assumed_utc,
            last_available_5m_open_utc=None,
            last_available_5m_close_utc=None,
            direction="UNCLEAR",
            direction_since_utc=None,
            source_timeframe=SOURCE_TIMEFRAME,
            structure_event=None,
            causality_pass=True,
            reason="NO_CLOSED_CANDLE",
            warmup_bars_required=warmup_bars,
            warmup_bars_available=0,
            scanner_variant=RESEARCH_MATRIX[0]["name"],
        )

    last = structure.iloc[-1]
    last_open = ensure_utc_timestamp(last["timestamp"])
    last_close = ensure_utc_timestamp(last.get("candle_close_ts") or (last_open + pd.Timedelta(minutes=5)))
    if last_close > decision:
        raise TrendDirectionAtError(
            "LOOKAHEAD_VIOLATION",
            f"last candle close {last_close} > decision {decision}",
        )

    if n < warmup_bars:
        return TrendDirectionResult(
            symbol=symbol,
            exchange=exchange,
            requested_at_utc=_iso_z(decision) or "",
            timestamp_assumed_utc=timestamp_assumed_utc,
            last_available_5m_open_utc=_iso_z(last_open),
            last_available_5m_close_utc=_iso_z(last_close),
            direction="UNCLEAR",
            direction_since_utc=None,
            source_timeframe=SOURCE_TIMEFRAME,
            structure_event=None,
            causality_pass=True,
            reason="INSUFFICIENT_WARMUP",
            warmup_bars_required=warmup_bars,
            warmup_bars_available=n,
            protected_high=float(last["protected_high"]) if pd.notna(last.get("protected_high")) else None,
            protected_low=float(last["protected_low"]) if pd.notna(last.get("protected_low")) else None,
            major_direction=int(last["major_direction"]) if pd.notna(last.get("major_direction")) else 0,
            protected_structure_state=str(last.get("protected_structure_state") or ""),
            scanner_variant=RESEARCH_MATRIX[0]["name"],
        )

    major = int(last["major_direction"]) if pd.notna(last.get("major_direction")) else 0
    state = str(last.get("protected_structure_state") or "")
    direction = map_structure_to_direction(major, state)
    since, event = _direction_since_and_event(structure, direction=direction, major=major)
    reason = reason_for_direction(
        direction=direction, major=major, state=state, n=n, warmup_bars=warmup_bars
    )

    htf: dict[str, str] | None = None
    if include_htf and candles_5m is not None and not candles_5m.empty:
        htf = {
            "15m": _htf_direction_from_5m(candles_5m, decision, "15m"),
            "30m": _htf_direction_from_5m(candles_5m, decision, "30m"),
            "1h": "UNCLEAR",  # not stored; not required for primary 5m decision
            "4h": "UNCLEAR",
        }

    return TrendDirectionResult(
        symbol=symbol,
        exchange=exchange,
        requested_at_utc=_iso_z(decision) or "",
        timestamp_assumed_utc=timestamp_assumed_utc,
        last_available_5m_open_utc=_iso_z(last_open),
        last_available_5m_close_utc=_iso_z(last_close),
        direction=direction,
        direction_since_utc=since,
        source_timeframe=SOURCE_TIMEFRAME,
        structure_event=event,
        causality_pass=True,
        reason=reason,
        warmup_bars_required=warmup_bars,
        warmup_bars_available=n,
        protected_high=float(last["protected_high"]) if pd.notna(last.get("protected_high")) else None,
        protected_low=float(last["protected_low"]) if pd.notna(last.get("protected_low")) else None,
        major_direction=major,
        protected_structure_state=str(last.get("protected_structure_state") or ""),
        scanner_variant=RESEARCH_MATRIX[0]["name"],
        htf_context=htf,
    )


def load_mysql_5m_as_of(
    *,
    symbol: str,
    decision_time: pd.Timestamp,
    exchange: str = "bybit",
    env_file: str | None = None,
) -> tuple[pd.DataFrame, pd.Timestamp | None, pd.Timestamp | None]:
    """Load closed 5m candles with close_time <= decision_time.

    Returns (candles, data_first_open, data_last_close) where data bounds are
    from the full series (for coverage checks).
    """
    from pathlib import Path

    from research.regime_scanner.candle_sources import (
        MySQLCandleSource,
        load_regime_db_env_file,
    )

    if env_file:
        load_regime_db_env_file(Path(env_file))
    else:
        load_regime_db_env_file()

    src = MySQLCandleSource(exchange_default=exchange)
    try:
        # Full series bounds (closed only, no decision filter)
        full = src.load_candles(
            exchange=exchange,
            symbol=symbol,
            timeframe="5m",
            closed_only=True,
        )
        if full.empty:
            raise TrendDirectionAtError(
                "SYMBOL_NOT_FOUND",
                f"no 5m candles in MySQL for {exchange} {symbol}",
            )
        first_open = ensure_utc_timestamp(full["timestamp"].iloc[0])
        if "close_time" in full.columns:
            last_close = ensure_utc_timestamp(full["close_time"].iloc[-1])
        else:
            last_close = first_open  # overwritten below
            last_close = ensure_utc_timestamp(full["timestamp"].iloc[-1]) + pd.Timedelta(minutes=5)

        decision = ensure_utc_timestamp(decision_time)
        if decision < first_open:
            raise TrendDirectionAtError(
                "TIMESTAMP_BEFORE_DATA",
                f"timestamp { _iso_z(decision) } is before first candle open { _iso_z(first_open) }",
            )
        if decision > last_close:
            raise TrendDirectionAtError(
                "TIMESTAMP_AFTER_DATA",
                f"timestamp { _iso_z(decision) } is after last closed candle { _iso_z(last_close) }; "
                "not a historical live decision within data coverage",
            )

        asof = src.load_candles(
            exchange=exchange,
            symbol=symbol,
            timeframe="5m",
            decision_time=decision,
            closed_only=True,
        )
        if asof.empty:
            raise TrendDirectionAtError(
                "NO_CLOSED_CANDLE",
                f"no closed 5m candle with close_time <= { _iso_z(decision) }",
            )
        # Causality assert
        if "close_time" in asof.columns:
            max_close = ensure_utc_timestamp(asof["close_time"].max())
            if max_close > decision:
                raise TrendDirectionAtError(
                    "LOOKAHEAD_VIOLATION",
                    f"loaded candle close {max_close} > decision {decision}",
                )
        return asof, first_open, last_close
    finally:
        src.close()


def query_trend_direction_at(
    *,
    symbol: str,
    timestamp: object,
    exchange: str = "bybit",
    timeframe: str = "5m",
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    env_file: str | None = None,
    include_htf: bool = False,
    candles: pd.DataFrame | None = None,
) -> TrendDirectionResult:
    """Main API: symbol + timestamp → BULLISH/BEARISH/UNCLEAR."""
    sym = normalize_symbol(symbol)
    tf = str(timeframe).strip().lower()
    if tf != "5m":
        raise TrendDirectionAtError(
            "UNSUPPORTED_TIMEFRAME",
            f"primary direction timeframe must be 5m, got {timeframe!r}",
        )
    decision, assumed = parse_decision_timestamp(timestamp)

    if candles is None:
        candles, _first, _last = load_mysql_5m_as_of(
            symbol=sym,
            decision_time=decision,
            exchange=exchange,
            env_file=env_file,
        )
    else:
        # Test / injected path: filter close_time <= decision
        frame = candles.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        if "close_time" not in frame.columns:
            frame["close_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
        else:
            frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
        frame = frame.loc[frame["close_time"] <= decision].copy()
        if frame.empty:
            raise TrendDirectionAtError(
                "NO_CLOSED_CANDLE",
                f"no closed 5m candle with close_time <= { _iso_z(decision) }",
            )
        candles = frame

    # Canonical columns for scanner
    ohlcv = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(candles["timestamp"], utc=True),
            "open": pd.to_numeric(candles["open"], errors="coerce"),
            "high": pd.to_numeric(candles["high"], errors="coerce"),
            "low": pd.to_numeric(candles["low"], errors="coerce"),
            "close": pd.to_numeric(candles["close"], errors="coerce"),
            "volume": pd.to_numeric(candles["volume"], errors="coerce"),
        }
    )
    structure = run_c34b_on_ohlcv(ohlcv)
    return decide_from_structure(
        structure,
        decision_time=decision,
        symbol=sym,
        exchange=exchange,
        timestamp_assumed_utc=assumed,
        warmup_bars=warmup_bars,
        candles_5m=ohlcv,
        include_htf=include_htf,
    )


def format_text_report(result: TrendDirectionResult) -> str:
    lines = [
        f"symbol: {result.symbol}",
        f"requested_at_utc: {result.requested_at_utc}",
    ]
    if result.timestamp_assumed_utc:
        lines.append("timestamp_note: naive timestamp interpreted as UTC")
    lines += [
        f"last_5m_open_utc: {result.last_available_5m_open_utc}",
        f"last_5m_close_utc: {result.last_available_5m_close_utc}",
        f"direction: {result.direction}",
        f"direction_since_utc: {result.direction_since_utc}",
        f"source_timeframe: {result.source_timeframe}",
        f"structure_event: {result.structure_event}",
        f"causality_pass: {str(result.causality_pass).lower()}",
    ]
    if result.reason:
        lines.append(f"reason: {result.reason}")
    lines.append(f"warmup_bars: {result.warmup_bars_available}/{result.warmup_bars_required}")
    return "\n".join(lines)
