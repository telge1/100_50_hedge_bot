"""Long-history 5m OHLC from ClickHouse (same resample as PD1 landscape window)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BT_D3 = ROOT / "tools/phase_bt_d3_canonical_depth2_provider_v1"
if str(BT_D3) not in sys.path:
    sys.path.insert(0, str(BT_D3))

from validation_config_v1 import SYMBOL  # noqa: E402


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iter_5m_grid(start: datetime, end: datetime) -> list[datetime]:
    out: list[datetime] = []
    t = start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=5)
    return out


def load_5m_ohlc_ch(start_utc: str, end_utc: str) -> tuple[list[str], dict[str, dict[str, float]], dict[str, Any]]:
    """Load closed 5m bars [start_utc, end_utc] inclusive on bar timestamp."""
    from ssot_bootstrap_v1 import ensure_ssot_import_path

    ensure_ssot_import_path()
    from pd1_landscape_viewer_v1.historical_window_v1 import _resample_5m_from_candles
    from research_charts.clickhouse_source import ClickHouseResearchCandleSource
    from research_charts.historical_pool_state_v1 import static_timeline_import_bounds
    from research_charts.short_pool_oracle_v1 import parse_decision_iso

    start = _parse_utc(start_utc)
    end = _parse_utc(end_utc)
    if start > end:
        raise ValueError("start > end")

    published_lo = datetime(2026, 4, 1, tzinfo=timezone.utc)
    published_hi = datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc)
    try:
        from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope
        from research_charts.historical_pool_state_v1 import get_published_decision_closes
        from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import PublishedActiveReader

        hcfg = resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)
        reader = PublishedActiveReader()
        published = get_published_decision_closes(reader, hcfg.config_hash, pipeline_line_id=hcfg.pipeline_line_id)
        published_lo, published_hi = static_timeline_import_bounds(published)
    except Exception:
        pass

    meta: dict[str, Any] = {
        "symbol": SYMBOL,
        "source": "ClickHouseResearchCandleSource.get_candles(5m strict_complete_buckets)",
        "resample": "pd1_landscape_viewer_v1._resample_5m_from_candles",
        "data_bounds_lo": _iso(published_lo),
        "data_bounds_hi": _iso(published_hi),
        "requested_start": start_utc,
        "requested_end": end_utc,
    }
    if start < published_lo or end > published_hi:
        meta["out_of_import_bounds"] = True

    src = ClickHouseResearchCandleSource()
    candles = src.get_candles(SYMBOL, "5m", start=start, end=end + timedelta(minutes=5))
    bars_map = _resample_5m_from_candles(candles)
    grid = iter_5m_grid(start, end)
    prices: dict[str, dict[str, float]] = {}
    for t in grid:
        if t not in bars_map:
            continue
        b = bars_map[t]
        key = _iso(t)
        prices[key] = {
            "o": float(b["open"]),
            "h": float(b["high"]),
            "l": float(b["low"]),
            "c": float(b["close"]),
        }
    bar_keys = sorted(prices.keys(), key=parse_decision_iso)
    meta["bars_loaded"] = len(bar_keys)
    meta["grid_steps"] = len(grid)
    return bar_keys, prices, meta
