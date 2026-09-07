"""Read-only footprint load: candles_1m OHLC + public_trades_canonical levels."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config

from .aggregation import build_candle
from .contracts import (
    BUCKET_STEP,
    CANDLE_SECONDS,
    CANDLES_FQN,
    DEFAULT_BUFFER_SECONDS,
    MAX_CANDLES,
    MAX_RANGE_SECONDS,
    QUERY_TIMEOUT_S,
    SUPPORTED_MODE,
    SUPPORTED_SYMBOL,
    SUPPORTED_TIMEFRAME,
    TRADES_FQN,
    CoverageStatus,
)
from .coverage import (
    CandleTradePresence,
    classify_candle,
    classify_window,
    overlaps_known_gap,
)

logger = logging.getLogger(__name__)


class FootprintRequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _client():
    cfg = load_clickhouse_config()
    return clickhouse_connect.get_client(**cfg.connect_kwargs())


def validate_request(
    *,
    symbol: str,
    timeframe: str,
    mode: str,
    bucket_step: float,
    start: int,
    end: int,
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    if sym != SUPPORTED_SYMBOL:
        raise FootprintRequestError(
            "unsupported_symbol",
            f"Footprint MVP supports only {SUPPORTED_SYMBOL}",
        )
    tf = str(timeframe or "").strip().lower()
    if tf != SUPPORTED_TIMEFRAME:
        raise FootprintRequestError(
            "unsupported_timeframe",
            f"Footprint MVP supports only {SUPPORTED_TIMEFRAME}",
        )
    md = str(mode or "").strip().upper()
    if md != SUPPORTED_MODE:
        raise FootprintRequestError(
            "unsupported_mode",
            f"Footprint MVP supports only mode={SUPPORTED_MODE}",
        )
    try:
        step = float(bucket_step)
    except (TypeError, ValueError) as exc:
        raise FootprintRequestError("bad_bucket_step", "bucket_step must be 5.0") from exc
    if abs(step - float(BUCKET_STEP)) > 1e-12:
        raise FootprintRequestError("bad_bucket_step", "bucket_step must be 5.0")

    try:
        a = int(start)
        b = int(end)
    except (TypeError, ValueError) as exc:
        raise FootprintRequestError("bad_range", "from/to must be UTC unix seconds") from exc
    if a <= 0 or b <= 0:
        raise FootprintRequestError("bad_range", "from/to must be positive UTC unix seconds")
    if a >= b:
        raise FootprintRequestError("bad_range", "from must be < to")
    span = b - a
    if span > MAX_RANGE_SECONDS:
        raise FootprintRequestError(
            "range_too_large",
            f"max range is {MAX_RANGE_SECONDS} seconds",
        )
    max_candles_est = (span + CANDLE_SECONDS - 1) // CANDLE_SECONDS
    if max_candles_est > MAX_CANDLES:
        raise FootprintRequestError(
            "too_many_candles",
            f"max {MAX_CANDLES} candles per request",
        )
    return {
        "symbol": sym,
        "timeframe": tf,
        "mode": md,
        "bucket_step": float(BUCKET_STEP),
        "start": a,
        "end": b,
    }


def _dt(unix: int) -> datetime:
    return datetime.fromtimestamp(int(unix), tz=timezone.utc)


def _unix_from_ch(value: Any) -> int:
    """Interpret ClickHouse datetime values as UTC wall clock."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return int(value.replace(tzinfo=timezone.utc).timestamp())
        return int(value.astimezone(timezone.utc).timestamp())
    raise TypeError(f"unsupported datetime value: {type(value)!r}")


def fetch_ohlc_5m(client: Any, symbol: str, start: int, end: int) -> list[dict[str, float]]:
    """Aggregate signal_generator.candles_1m to 5m OHLC (SoT for candle bodies)."""
    rows = client.query(
        f"""
        SELECT
          toStartOfFiveMinutes(open_time) AS t,
          argMin(open, open_time) AS o,
          max(high) AS h,
          min(low) AS l,
          argMax(close, open_time) AS c
        FROM {CANDLES_FQN}
        WHERE symbol = {{s:String}}
          AND interval = '1m'
          AND open_time >= {{a:DateTime64(3,'UTC')}}
          AND open_time < {{b:DateTime64(3,'UTC')}}
        GROUP BY t
        ORDER BY t
        """,
        parameters={"s": symbol, "a": _dt(start), "b": _dt(end)},
        settings={"max_execution_time": int(QUERY_TIMEOUT_S)},
    ).result_rows
    out: list[dict[str, float]] = []
    for t, o, h, l, c in rows:
        out.append(
            {
                "time": _unix_from_ch(t),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
            }
        )
    return out


def fetch_level_rows(
    client: Any, symbol: str, start: int, end: int, bucket_step: float
) -> list[tuple]:
    """Deduped (symbol, trade_id) then bucketed ask/bid size+notional per 5m candle."""
    rows = client.query(
        f"""
        WITH dedup AS (
          SELECT
            argMax(trade_ts, ingest_timestamp) AS ts,
            argMax(side, ingest_timestamp) AS sd,
            argMax(toFloat64(price), ingest_timestamp) AS px,
            argMax(toFloat64(size), ingest_timestamp) AS sz,
            argMax(toFloat64(notional), ingest_timestamp) AS nt,
            argMax(source, ingest_timestamp) AS src
          FROM {TRADES_FQN}
          PREWHERE symbol = {{s:String}}
          WHERE trade_ts >= {{a:DateTime64(3,'UTC')}}
            AND trade_ts < {{b:DateTime64(3,'UTC')}}
          GROUP BY symbol, trade_id
        )
        SELECT
          toUnixTimestamp(toStartOfFiveMinutes(ts)) AS candle_time,
          toInt64(floor(px / {{step:Float64}})) AS bucket_index,
          sumIf(sz, sd = 'Buy') AS ask_size,
          sumIf(sz, sd = 'Sell') AS bid_size,
          sumIf(nt, sd = 'Buy') AS ask_notional,
          sumIf(nt, sd = 'Sell') AS bid_notional,
          count() AS trade_count,
          groupUniqArray(src) AS sources
        FROM dedup
        GROUP BY candle_time, bucket_index
        ORDER BY candle_time, bucket_index
        """,
        parameters={
            "s": symbol,
            "a": _dt(start),
            "b": _dt(end),
            "step": float(bucket_step),
        },
        settings={"max_execution_time": int(QUERY_TIMEOUT_S)},
    ).result_rows
    return list(rows)


def _group_levels(
    level_rows: list[tuple],
) -> dict[int, tuple[dict, list[str], int]]:
    """candle_time → (by_idx RawLevelAgg via levels_from_ch_rows inputs, sources, trades)."""
    from .aggregation import RawLevelAgg

    by_candle: dict[int, dict[int, RawLevelAgg]] = {}
    sources_by: dict[int, set[str]] = {}
    trades_by: dict[int, int] = {}

    for row in level_rows:
        ct = int(row[0])
        idx = int(row[1])
        ask_s = float(row[2] or 0.0)
        bid_s = float(row[3] or 0.0)
        ask_n = float(row[4] or 0.0)
        bid_n = float(row[5] or 0.0)
        trades = int(row[6] or 0)
        srcs = row[7] if len(row) > 7 else []
        if isinstance(srcs, (list, tuple)):
            src_list = [str(x) for x in srcs]
        else:
            src_list = [str(srcs)] if srcs else []

        bucket = by_candle.setdefault(ct, {})
        bucket[idx] = RawLevelAgg(
            bucket_index=idx,
            ask_size=ask_s,
            bid_size=bid_s,
            ask_notional=ask_n,
            bid_notional=bid_n,
            trade_count=trades,
        )
        sources_by.setdefault(ct, set()).update(src_list)
        trades_by[ct] = trades_by.get(ct, 0) + trades

    out: dict[int, tuple[dict, list[str], int]] = {}
    for ct, levels in by_candle.items():
        out[ct] = (levels, sorted(sources_by.get(ct, ())), int(trades_by.get(ct, 0)))
    return out


def load_footprint(
    *,
    symbol: str,
    timeframe: str,
    mode: str,
    bucket_step: float,
    start: int,
    end: int,
    client: Any | None = None,
    positive_complete: bool = False,
    include_avr: bool = True,
) -> dict[str, Any]:
    req = validate_request(
        symbol=symbol,
        timeframe=timeframe,
        mode=mode,
        bucket_step=bucket_step,
        start=start,
        end=end,
    )
    own_client = client is None
    client = client or _client()
    try:
        try:
            ohlc = fetch_ohlc_5m(client, req["symbol"], req["start"], req["end"])
            level_rows = fetch_level_rows(
                client, req["symbol"], req["start"], req["end"], req["bucket_step"]
            )
        except Exception as exc:  # noqa: BLE001 — map to safe API error
            logger.exception("footprint query failed")
            raise FootprintRequestError(
                "query_failed",
                "Footprint query failed; try a smaller range",
            ) from exc

        grouped = _group_levels(level_rows)
        candles_out = []
        presence: list[CandleTradePresence] = []

        ohlc_times = {int(c["time"]) for c in ohlc}
        # Include trade-only candles? Spec: OHLC from candles_1m is SoT —
        # only emit candles that have OHLC; trades without OHLC noted in meta.
        orphan_trade_candles = sorted(set(grouped.keys()) - ohlc_times)

        for c in ohlc:
            t = int(c["time"])
            levels, sources, trade_count = grouped.get(t, ({}, [], 0))
            # Known gap overlap without trades strengthens MISSING.
            c_end = t + CANDLE_SECONDS
            if trade_count <= 0 and overlaps_known_gap(t, c_end):
                cov = CoverageStatus.MISSING
            else:
                cov = classify_candle(
                    has_ohlc=True,
                    trade_count=trade_count,
                    sources=sources,
                    positive_complete=positive_complete,
                )
            presence.append(
                CandleTradePresence(
                    time=t,
                    has_ohlc=True,
                    trade_count=trade_count,
                    sources=tuple(sources),
                )
            )
            if cov == CoverageStatus.MISSING:
                # No invented levels.
                candles_out.append(
                    build_candle(
                        time=t,
                        open_=c["open"],
                        high=c["high"],
                        low=c["low"],
                        close=c["close"],
                        by_idx={},
                        coverage=cov.value,
                        sources=sources,
                    ).to_dict()
                )
            else:
                candles_out.append(
                    build_candle(
                        time=t,
                        open_=c["open"],
                        high=c["high"],
                        low=c["low"],
                        close=c["close"],
                        by_idx=levels,
                        coverage=cov.value,
                        sources=sources,
                    ).to_dict()
                )

        window_cov = classify_window(presence, positive_complete=positive_complete)
        payload = {
            "success": True,
            "symbol": req["symbol"],
            "timeframe": req["timeframe"],
            "mode": req["mode"],
            "bucket_step": req["bucket_step"],
            "ohlc_source": CANDLES_FQN,
            "trades_source": TRADES_FQN,
            "availability_clock": "ingest_timestamp",
            "range": {"from": req["start"], "to": req["end"]},
            "coverage": window_cov.value,
            "coverage_note": _coverage_note(window_cov),
            "meta": {
                "candles": len(candles_out),
                "orphan_trade_candles": orphan_trade_candles,
                "buffer_hint_s": DEFAULT_BUFFER_SECONDS,
                "imbalance_highlights": window_cov == CoverageStatus.COMPLETE,
                "positive_complete": positive_complete,
            },
            "candles": candles_out,
        }
        if include_avr:
            from .response_service import attach_avr_to_footprint

            try:
                attach_avr_to_footprint(payload, client=client)
            except Exception:  # noqa: BLE001 — footprint still usable without AVR
                logger.exception("avr attach failed")
                payload["avr_engine"] = {
                    "enabled": True,
                    "success": False,
                    "error": "avr_attach_failed",
                }
        return payload
    finally:
        if own_client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _coverage_note(status: CoverageStatus) -> str:
    if status == CoverageStatus.COMPLETE:
        return "Coverage positively confirmed"
    if status == CoverageStatus.PARTIAL:
        return "Partial / backfilled data — not confirmed"
    if status == CoverageStatus.MISSING:
        return "Footprint-Daten fehlen"
    return "Coverage unknown — not confirmed"


# Re-export for tests
__all__ = [
    "FootprintRequestError",
    "validate_request",
    "load_footprint",
    "fetch_ohlc_5m",
    "fetch_level_rows",
]
