"""15m bar-gap verification for pilot week (read-only parquet)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from data.timeframes import aggregate, timeframe_seconds
from indicators.live_micro_lld_v2.availability import bar_close

from ..immutable_candle_snapshot_v1 import SnapshotError, load_snapshot
from .pilot_config import CATALOG_ROOT, PILOT_RANGE_END_EXCLUSIVE, PILOT_RANGE_START, PILOT_TIMEFRAME

FROZEN_DIRS = {
    "84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93": CATALOG_ROOT / "inputs",
    "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b": CATALOG_ROOT.parent
    / "doge_multi_month_2026_history"
    / "inputs",
}


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rows_to_candles(rows) -> list:
    from data.models import Candle

    out = []
    for r in rows:
        out.append(
            Candle(
                timestamp=_utc(r.open_time),
                open=float(r.open),
                high=float(r.high),
                low=float(r.low),
                close=float(r.close),
                volume=float(r.volume),
                symbol="DOGEUSDT",
                timeframe="1m",
            )
        )
    return out


def verify_pilot_15m_bars(frozen_id: str) -> dict[str, Any]:
    directory = FROZEN_DIRS.get(frozen_id)
    if directory is None:
        return {"frozen_id": frozen_id, "level": "UNPROVEN", "error": "unknown frozen id"}
    try:
        rows = load_snapshot(directory, frozen_id)
    except SnapshotError as exc:
        return {"frozen_id": frozen_id, "level": "UNPROVEN", "error": str(exc)}

    candles_1m = _rows_to_candles(rows)
    start = _utc(datetime.fromisoformat(PILOT_RANGE_START.replace("Z", "+00:00")))
    end = _utc(datetime.fromisoformat(PILOT_RANGE_END_EXCLUSIVE.replace("Z", "+00:00")))
    step = timeframe_seconds(PILOT_TIMEFRAME)
    window_1m = [c for c in candles_1m if start <= _utc(c.timestamp) < end + timedelta(seconds=step)]
    if not window_1m:
        return {
            "frozen_id": frozen_id,
            "level": "INCOMPLETE",
            "error": "no 1m rows in pilot window",
        }
    agg = aggregate(window_1m, PILOT_TIMEFRAME)
    missing_slots: list[str] = []
    bad_ohlc: list[str] = []
    opens = sorted(int(_utc(c.timestamp).timestamp()) for c in agg)
    if opens:
        t = opens[0]
        end_ts = int(end.timestamp())
        expected = set()
        while t < end_ts:
            expected.add(t)
            t += step
        got = set(opens)
        missing_slots = [
            datetime.fromtimestamp(x, tz=timezone.utc).isoformat()
            for x in sorted(expected - got)
        ][:20]
    for c in agg:
        if c.high is None or c.low is None or c.high < c.low:
            bad_ohlc.append(_utc(c.timestamp).isoformat())
    level = "COMPLETE_BAR_VERIFIED" if not missing_slots and not bad_ohlc else "INCOMPLETE"
    return {
        "frozen_id": frozen_id,
        "level": level,
        "bars_15m": len(agg),
        "missing_15m_slots_sample": missing_slots,
        "bad_ohlc_count": len(bad_ohlc),
        "first_bar_close_semantics": bar_close(_utc(agg[0].timestamp), PILOT_TIMEFRAME).isoformat()
        if agg
        else None,
    }


def verify_pilot_frozen_inputs() -> dict[str, Any]:
    combo = CATALOG_ROOT / "production/results/2026-08/15m_PD10"
    frozen_ids = set()
    for p in combo.glob("2026080*.json"):
        payload = json.loads(p.read_text(encoding="utf-8"))
        fid = str((payload.get("frozen_input") or {}).get("snapshot_id") or "")
        if fid:
            frozen_ids.add(fid)
    results = {fid: verify_pilot_15m_bars(fid) for fid in sorted(frozen_ids)}
    return {"pilot_frozen_ids": list(frozen_ids), "bar_checks": results}
