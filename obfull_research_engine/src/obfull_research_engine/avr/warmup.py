"""Warmup / future coverage checks for AVR adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from orderbook_analyse.research.general_market_behavior_v1.coverage import load_clickhouse_env

from ..interval_coverage import _q
from ..timeparse import format_utc_z
from .provenance import ensure_avr_import_path


def check_avr_warmup_coverage(
    *,
    symbol: str,
    feature_start: datetime,
    feature_end: datetime,
) -> dict[str, Any]:
    """Require public-trade history covering [start−lookback, end) for AVR baselines."""
    ensure_avr_import_path()
    from footprint_candles.response_contracts import (  # noqa: WPS433
        BASELINE_LOOKBACK_S,
        MIN_BASELINE_VALID_SECONDS,
        PRIMARY_WINDOW_S,
    )

    load_clickhouse_env()
    symbol = symbol.upper()
    feature_start = feature_start.astimezone(timezone.utc)
    feature_end = feature_end.astimezone(timezone.utc)
    preroll_start = feature_start - timedelta(seconds=int(BASELINE_LOOKBACK_S))
    # Extra window before preroll for feature windows at early preroll points
    load_start = preroll_start - timedelta(seconds=int(PRIMARY_WINDOW_S))

    tip_raw = _q(
        "SELECT max(trade_ts) FROM orderbook_analysis.public_trades_canonical "
        f"WHERE symbol='{symbol}' FORMAT TSV"
    ).strip()
    tip_ok = False
    tip_z = None
    if tip_raw and not tip_raw.startswith("1970"):
        tip_dt = datetime.fromisoformat(tip_raw.replace("Z", "+00:00"))
        if tip_dt.tzinfo is None:
            tip_dt = tip_dt.replace(tzinfo=timezone.utc)
        tip_ok = tip_dt >= feature_end
        tip_z = tip_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    hs = load_start.strftime("%Y-%m-%d %H:%M:%S")
    he = feature_end.strftime("%Y-%m-%d %H:%M:%S")
    ps = preroll_start.strftime("%Y-%m-%d %H:%M:%S")
    pe = feature_start.strftime("%Y-%m-%d %H:%M:%S")
    n_load = int(
        _q(
            "SELECT count() FROM orderbook_analysis.public_trades_canonical "
            f"WHERE symbol='{symbol}' AND trade_ts>='{hs}' AND trade_ts<'{he}' FORMAT TSV"
        )
        or 0
    )
    n_preroll = int(
        _q(
            "SELECT count() FROM orderbook_analysis.public_trades_canonical "
            f"WHERE symbol='{symbol}' AND trade_ts>='{ps}' AND trade_ts<'{pe}' FORMAT TSV"
        )
        or 0
    )
    n_preroll_seconds = int(
        _q(
            "SELECT uniqExact(toStartOfSecond(trade_ts)) FROM orderbook_analysis.public_trades_canonical "
            f"WHERE symbol='{symbol}' AND trade_ts>='{ps}' AND trade_ts<'{pe}' FORMAT TSV"
        )
        or 0
    )
    # Fail-closed: tip must cover feature end; preroll must have enough distinct seconds
    ok = tip_ok and n_preroll > 0 and n_preroll_seconds >= int(MIN_BASELINE_VALID_SECONDS)
    reason = None
    if not tip_ok:
        reason = "AVR_TIP_BEFORE_FEATURE_END"
    elif n_preroll <= 0:
        reason = "AVR_WARMUP_EMPTY"
    elif n_preroll_seconds < int(MIN_BASELINE_VALID_SECONDS):
        reason = "AVR_WARMUP_INSUFFICIENT_SECONDS"

    return {
        "ok": ok,
        "source": "AVR_WARMUP_COVERAGE",
        "symbol": symbol,
        "feature_start": format_utc_z(feature_start),
        "feature_end": format_utc_z(feature_end),
        "preroll_start": format_utc_z(preroll_start),
        "load_start": format_utc_z(load_start),
        "baseline_lookback_s": int(BASELINE_LOOKBACK_S),
        "min_baseline_valid_seconds": int(MIN_BASELINE_VALID_SECONDS),
        "primary_window_s": int(PRIMARY_WINDOW_S),
        "ch_tip_trade_ts": tip_z,
        "n_trades_load_window": n_load,
        "n_trades_preroll": n_preroll,
        "n_preroll_distinct_seconds": n_preroll_seconds,
        "reason": reason,
    }
