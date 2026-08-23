"""Coverage classification for multi-coin preflight (pure + ClickHouse-backed)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ....cluster_sweep_research.clickhouse_source import _as_utc, _q, coverage_report
from ....cluster_sweep_research.ema_features import required_warmup_bars
from ...config import EMA_DUAL_CROSS_DEFAULTS
from .candidate_coverage import classify_liq_feed, classify_oi_window, listing_audit
from .constants import (
    CANDLE_FULL_RATIO,
    ELIGIBILITY_MEANS_THRESHOLD_PASS_NOT_COMPLETE_COVERAGE,
    ELIGIBILITY_THRESHOLDS,
    ELIGIBLE_CORE_30D,
    ELIGIBLE_CORE_PARTIAL,
    EXPECTED_WINDOW_DAYS,
    INELIGIBLE_CORE,
    LISTING_LIMITED,
    OB_COMPLETE_DAY_RATIO,
    OB_DEPTH,
    OB_FULL_RATIO,
    OB_PARSER,
    OUTCOME_MIN_RATIO,
    TRADES_MIN_RATIO,
    WARMUP_BARS_EXTRA,
)


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def expected_minutes(start: datetime, end: datetime) -> int:
    return int((_utc(end) - _utc(start)).total_seconds() // 60)


def classify_coverage(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    candle_minutes: int,
    trades_minutes: int,
    ob_minutes: int,
    outcome_minutes: int,
    oi_minutes: int,
    oi_first_ts: datetime | str | None,
    oi_last_ts: datetime | str | None,
    liq_feed_first_ts: datetime | str | None,
    liq_feed_last_ts: datetime | str | None,
    liquidation_events: int,
    warmup_bars_available: int,
    warmup_bars_required: int,
    listing_audit_result: dict[str, Any],
    ob_parser: str = OB_PARSER,
    ob_depth: int = OB_DEPTH,
) -> dict[str, Any]:
    """Classify one coin. Performance metrics are intentionally absent."""
    start_u, end_u = _utc(start), _utc(end)
    exp = expected_minutes(start_u, end_u)
    candle_ratio = (candle_minutes / exp) if exp else 0.0
    ob_ratio = (ob_minutes / exp) if exp else 0.0
    trades_ratio = (trades_minutes / max(candle_minutes, 1)) if candle_minutes else 0.0
    outcome_ratio = (outcome_minutes / exp) if exp else 0.0

    listing_status = listing_audit_result.get("listing_status") or "UNKNOWN"
    listing_limited = listing_audit_result.get("listing_limited")
    listing_known = bool(listing_audit_result.get("listing_limited_known"))

    warmup_ok = warmup_bars_available >= warmup_bars_required
    core_present = candle_minutes > 0 and trades_minutes > 0 and ob_minutes > 0
    threshold_pass = (
        candle_ratio >= CANDLE_FULL_RATIO
        and ob_ratio >= OB_FULL_RATIO
        and trades_ratio >= TRADES_MIN_RATIO
        and outcome_ratio >= OUTCOME_MIN_RATIO
        and warmup_ok
        and ob_parser == OB_PARSER
        and int(ob_depth) == OB_DEPTH
    )

    # Only assert LISTING_LIMITED when listing is reliably known mid-window
    if listing_known and listing_limited is True and not threshold_pass:
        coverage_class = LISTING_LIMITED
    elif threshold_pass and not (listing_known and listing_limited is True):
        coverage_class = ELIGIBLE_CORE_30D  # threshold pass, not complete coverage
    elif core_present and warmup_ok:
        coverage_class = ELIGIBLE_CORE_PARTIAL
    elif listing_known and listing_limited is True:
        coverage_class = LISTING_LIMITED
    else:
        coverage_class = INELIGIBLE_CORE

    oi_block = classify_oi_window(
        oi_minutes=oi_minutes,
        expected_minutes=exp,
        oi_first_ts=oi_first_ts,
        oi_last_ts=oi_last_ts,
        window_start=start_u,
        window_end=end_u,
    )
    liq_block = classify_liq_feed(
        liq_feed_first_ts=liq_feed_first_ts,
        liq_feed_last_ts=liq_feed_last_ts,
        liquidation_events=liquidation_events,
        window_start=start_u,
        window_end=end_u,
    )

    return {
        "symbol": symbol,
        "coverage_class": coverage_class,
        "eligible_main": coverage_class == ELIGIBLE_CORE_30D,
        "eligibility_means_threshold_pass_not_complete_coverage": (
            ELIGIBILITY_MEANS_THRESHOLD_PASS_NOT_COMPLETE_COVERAGE
        ),
        "eligibility_thresholds": dict(ELIGIBILITY_THRESHOLDS),
        "candles_minutes": int(candle_minutes),
        "candles_expected": exp,
        "candles_coverage_ratio": round(candle_ratio, 6),
        "public_trades_minutes": int(trades_minutes),
        "public_trades_coverage_ratio": round(trades_ratio, 6),
        "orderbook_minutes": int(ob_minutes),
        "orderbook_coverage_ratio": round(ob_ratio, 6),
        "ob200_v3_parser": ob_parser,
        "ob200_v3_depth": int(ob_depth),
        "outcome_1m_minutes": int(outcome_minutes),
        "outcome_1m_coverage_ratio": round(outcome_ratio, 6),
        **oi_block,
        **liq_block,
        "warmup_bars_available": int(warmup_bars_available),
        "warmup_bars_required": int(warmup_bars_required),
        "warmup_ok": warmup_ok,
        "listing_status": listing_status,
        "listing_first_ts": listing_audit_result.get("listing_first_ts"),
        "listing_limited": listing_limited,
        "listing_limited_known": listing_known,
        "listing_note": listing_audit_result.get("listing_note"),
        "window_start": start_u.isoformat(),
        "window_end": end_u.isoformat(),
        "performance_used_for_eligibility": False,
    }


def _count_distinct_minutes(client, table_sql: str, params: dict[str, Any]) -> int:
    rows = _q(client, table_sql, params)
    return int(rows[0][0]) if rows else 0


def _minmax_ts(client, sql: str, params: dict[str, Any]) -> tuple[Any, Any, int]:
    rows = _q(client, sql, params)
    if not rows:
        return None, None, 0
    mn, mx, n = rows[0]
    return mn, mx, int(n or 0)


def probe_symbol_coverage(
    client,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    timeframe_for_warmup: str = "5m",
) -> dict[str, Any]:
    """ClickHouse-backed coverage probe for one symbol (used by --preflight-only / --run)."""
    start_u, end_u = _utc(start), _utc(end)
    exp_days = (end_u - start_u).total_seconds() / 86400.0
    window_note = f"window_days={exp_days}" if abs(exp_days - EXPECTED_WINDOW_DAYS) > 1e-6 else "window_days=30"

    report = coverage_report(client, symbol, start_u, end_u)

    candle_minutes = _count_distinct_minutes(
        client,
        """
        SELECT countDistinct(toStartOfMinute(open_time))
        FROM signal_generator.candles_1m FINAL
        WHERE symbol={s:String} AND interval='1m'
          AND open_time>={a:DateTime64(3,'UTC')} AND open_time<{b:DateTime64(3,'UTC')}
        """,
        {"s": symbol, "a": _as_utc(start_u), "b": _as_utc(end_u)},
    )
    trades_minutes = _count_distinct_minutes(
        client,
        """
        SELECT countDistinct(toStartOfMinute(trade_ts))
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol={s:String}
          AND trade_ts>={a:DateTime64(3,'UTC')} AND trade_ts<{b:DateTime64(3,'UTC')}
        """,
        {"s": symbol, "a": _as_utc(start_u), "b": _as_utc(end_u)},
    )
    ob_minutes = _count_distinct_minutes(
        client,
        """
        SELECT countDistinct(toStartOfMinute(bucket_start))
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE symbol={s:String}
          AND parser_version={pv:String} AND depth={d:UInt16}
          AND bucket_start>={a:DateTime64(3,'UTC')} AND bucket_start<{b:DateTime64(3,'UTC')}
        """,
        {
            "s": symbol,
            "pv": OB_PARSER,
            "d": OB_DEPTH,
            "a": _as_utc(start_u),
            "b": _as_utc(end_u),
        },
    )
    outcome_minutes = candle_minutes

    oi_first, oi_last, oi_minutes = _minmax_ts(
        client,
        """
        SELECT min(bucket_time), max(bucket_time), countDistinct(toStartOfMinute(bucket_time))
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol={s:String}
          AND bucket_time>={a:DateTime64(3,'UTC')} AND bucket_time<{b:DateTime64(3,'UTC')}
        """,
        {"s": symbol, "a": _as_utc(start_u), "b": _as_utc(end_u)},
    )

    liq_first, liq_last, liq_events = _minmax_ts(
        client,
        """
        SELECT min(event_time), max(event_time), count()
        FROM orderbook_analysis.all_liquidations
        WHERE symbol={s:String}
          AND event_time>={a:DateTime64(3,'UTC')} AND event_time<{b:DateTime64(3,'UTC')}
        """,
        {"s": symbol, "a": _as_utc(start_u), "b": _as_utc(end_u)},
    )

    tf_min = {"5m": 5, "15m": 15, "30m": 30}.get(timeframe_for_warmup, 5)
    need_bars = required_warmup_bars(EMA_DUAL_CROSS_DEFAULTS.ema_slow, WARMUP_BARS_EXTRA)
    warm_start = start_u - timedelta(minutes=need_bars * tf_min + 60)
    warm_rows = _q(
        client,
        """
        SELECT count()
        FROM signal_generator.candles_1m FINAL
        WHERE symbol={s:String} AND interval='1m'
          AND open_time>={a:DateTime64(3,'UTC')} AND open_time<{b:DateTime64(3,'UTC')}
        """,
        {"s": symbol, "a": _as_utc(warm_start), "b": _as_utc(start_u)},
    )
    warm_1m = int(warm_rows[0][0]) if warm_rows else 0
    warmup_bars_available = warm_1m // tf_min

    # Unbounded earliest candle for listing audit (not limited to research window)
    earliest_rows = _q(
        client,
        """
        SELECT min(open_time)
        FROM signal_generator.candles_1m FINAL
        WHERE symbol={s:String} AND interval='1m'
        """,
        {"s": symbol},
    )
    earliest_candle = earliest_rows[0][0] if earliest_rows else None
    window_bounded_first = (report.get("candles_1m") or {}).get("first_ts")
    listing = listing_audit(
        earliest_candle_unbounded=earliest_candle,
        window_start=start_u,
        window_bounded_first_ts=window_bounded_first,
    )

    classified = classify_coverage(
        symbol=symbol,
        start=start_u,
        end=end_u,
        candle_minutes=candle_minutes,
        trades_minutes=trades_minutes,
        ob_minutes=ob_minutes,
        outcome_minutes=outcome_minutes,
        oi_minutes=oi_minutes,
        oi_first_ts=oi_first,
        oi_last_ts=oi_last,
        liq_feed_first_ts=liq_first,
        liq_feed_last_ts=liq_last,
        liquidation_events=liq_events,
        warmup_bars_available=warmup_bars_available,
        warmup_bars_required=need_bars,
        listing_audit_result=listing,
    )
    classified["window_note"] = window_note
    classified["feeds_raw"] = {
        k: {kk: vv for kk, vv in (v or {}).items() if kk != "sample"}
        for k, v in report.items()
        if isinstance(v, dict)
    }
    classified["ob_complete_day_threshold"] = OB_COMPLETE_DAY_RATIO
    classified["feed_meta"] = {
        "oi_first_ts": classified.get("oi_first_ts"),
        "oi_last_ts": classified.get("oi_last_ts"),
        "liq_feed_first_ts": classified.get("liq_feed_first_ts"),
        "liq_feed_last_ts": classified.get("liq_feed_last_ts"),
        "oi_window_status": classified.get("oi_window_status"),
        "liq_feed_coverage_status": classified.get("liq_feed_coverage_status"),
    }
    return classified


def select_eligible_for_main(rows: list[dict[str, Any]]) -> list[str]:
    """Main analysis uses only ELIGIBLE_CORE_30D (threshold pass) — never performance filters."""
    return [r["symbol"] for r in rows if r.get("coverage_class") == ELIGIBLE_CORE_30D]


def partition_by_class(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {
        ELIGIBLE_CORE_30D: [],
        ELIGIBLE_CORE_PARTIAL: [],
        INELIGIBLE_CORE: [],
        LISTING_LIMITED: [],
    }
    for r in rows:
        cls = r.get("coverage_class") or INELIGIBLE_CORE
        out.setdefault(cls, []).append(r["symbol"])
    return out
