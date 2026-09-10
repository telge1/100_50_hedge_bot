"""Coverage gate for multiscale footprint context."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from orderbook_analyse.research.general_market_behavior_v1.coverage import load_clickhouse_env

from ..interval_coverage import _q
from ..timeparse import format_utc_z
from ..avr.provenance import ensure_avr_import_path
from ..avr.warmup import check_avr_warmup_coverage
from . import LOOKBACK_S


def check_multiscale_coverage(
    *,
    symbol: str,
    feature_start: datetime,
    feature_end: datetime,
) -> dict[str, Any]:
    """Fail-closed checks for PT lookback, AVR warmup, OI/liq lookback, closed 5m."""
    ensure_avr_import_path()
    load_clickhouse_env()
    symbol = symbol.upper()
    feature_start = feature_start.astimezone(timezone.utc)
    feature_end = feature_end.astimezone(timezone.utc)
    load_start = feature_start - timedelta(seconds=int(LOOKBACK_S))

    warmup = check_avr_warmup_coverage(
        symbol=symbol, feature_start=feature_start, feature_end=feature_end
    )

    hs = load_start.strftime("%Y-%m-%d %H:%M:%S")
    he = feature_end.strftime("%Y-%m-%d %H:%M:%S")
    n_pt = int(
        _q(
            "SELECT count() FROM orderbook_analysis.public_trades_canonical "
            f"WHERE symbol='{symbol}' AND trade_ts>='{hs}' AND trade_ts<'{he}' FORMAT TSV"
        )
        or 0
    )
    n_pt_lookback = int(
        _q(
            "SELECT count() FROM orderbook_analysis.public_trades_canonical "
            f"WHERE symbol='{symbol}' AND trade_ts>='{hs}' AND trade_ts<'{feature_start.strftime('%Y-%m-%d %H:%M:%S')}' FORMAT TSV"
        )
        or 0
    )

    # Closed 5m before feature_start must have some trades (09:55–10:00 for 10:00 start)
    closed_end = feature_start
    closed_start = feature_start - timedelta(seconds=300)
    cs = closed_start.strftime("%Y-%m-%d %H:%M:%S")
    ce = closed_end.strftime("%Y-%m-%d %H:%M:%S")
    n_closed_5m = int(
        _q(
            "SELECT count() FROM orderbook_analysis.public_trades_canonical "
            f"WHERE symbol='{symbol}' AND trade_ts>='{cs}' AND trade_ts<'{ce}' FORMAT TSV"
        )
        or 0
    )

    reasons: list[str] = []
    if not warmup.get("ok"):
        reasons.append(f"AVR_WARMUP:{warmup.get('reason')}")
    if n_pt_lookback <= 0:
        reasons.append("PUBLIC_TRADES_LOOKBACK_EMPTY")
        reasons.append(
            f"missing_interval={format_utc_z(load_start)}–{format_utc_z(feature_start)}"
        )
    if n_closed_5m <= 0:
        reasons.append("CLOSED_5M_BEFORE_START_EMPTY")
        reasons.append(f"missing_interval={format_utc_z(closed_start)}–{format_utc_z(closed_end)}")

    ok = len(reasons) == 0
    return {
        "ok": ok,
        "source": "AVR_MULTISCALE_COVERAGE",
        "symbol": symbol,
        "feature_start": format_utc_z(feature_start),
        "feature_end": format_utc_z(feature_end),
        "load_start": format_utc_z(load_start),
        "lookback_s": LOOKBACK_S,
        "n_public_trades_load_window": n_pt,
        "n_public_trades_lookback": n_pt_lookback,
        "n_public_trades_closed_5m_before_start": n_closed_5m,
        "closed_5m_before_start": {
            "start": format_utc_z(closed_start),
            "end": format_utc_z(closed_end),
        },
        "avr_warmup": warmup,
        "reason": None if ok else ";".join(reasons),
        "verdict": "DATA_COMPLETE" if ok else "DATA_NOT_COMPLETE",
    }
