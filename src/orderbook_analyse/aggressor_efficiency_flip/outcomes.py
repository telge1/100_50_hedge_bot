"""Forward diagnostic outcomes — never feed features/gates."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket
from orderbook_analyse.aggressor_efficiency_flip.timeutil import floor_second, iso_z, parse_utc


HORIZONS_S = (30, 60, 180, 300, 900, 1800, 3600)


def _price_at(buckets: dict[datetime, SecondBucket], ts: datetime) -> Optional[float]:
    b = buckets.get(floor_second(ts))
    if b and b.last_price is not None:
        return b.last_price
    # search forward/back a few seconds for coverage
    for d in range(1, 6):
        for sgn in (1, -1):
            bb = buckets.get(floor_second(ts) + timedelta(seconds=sgn * d))
            if bb and bb.last_price is not None:
                return bb.last_price
    return None


def compute_outcomes(
    candidates: list[dict[str, Any]],
    buckets: dict[datetime, SecondBucket],
    *,
    data_end: datetime,
) -> list[dict[str, Any]]:
    """Outcomes start strictly after diagnostic_earliest_entry_ts."""
    out: list[dict[str, Any]] = []
    data_end = floor_second(data_end)
    for c in candidates:
        entry_ts_s = c.get("diagnostic_earliest_entry_ts")
        entry_px = c.get("diagnostic_earliest_entry_price")
        if not entry_ts_s or entry_px is None:
            continue
        entry_ts = parse_utc(entry_ts_s)
        final_ts = parse_utc(c["final_decision_ts"])
        if not (entry_ts > final_ts):
            raise AssertionError("outcome_entry_not_after_final")
        row: dict[str, Any] = {
            "episode_id": c["episode_id"],
            "direction": c["direction"],
            "diagnostic_earliest_entry_ts": entry_ts_s,
            "diagnostic_earliest_entry_price": entry_px,
        }
        direction = c["direction"]
        for h in HORIZONS_S:
            end_h = entry_ts + timedelta(seconds=h)
            key = f"forward_return_{h}s_bps"
            if end_h > data_end:
                row[key] = None
                row[f"mfe_{h}s_bps"] = None
                row[f"mae_{h}s_bps"] = None
                continue
            # path from entry_ts to end_h using closed buckets (entry_ts, end_h]
            mfe = mae = 0.0
            last = float(entry_px)
            cur = floor_second(entry_ts)
            while cur < end_h:
                b = buckets.get(cur)
                if b and b.high_price is not None and b.low_price is not None:
                    if direction == "LONG":
                        mfe = max(mfe, (b.high_price - entry_px) / entry_px * 1e4)
                        mae = max(mae, (entry_px - b.low_price) / entry_px * 1e4)
                    else:
                        mfe = max(mfe, (entry_px - b.low_price) / entry_px * 1e4)
                        mae = max(mae, (b.high_price - entry_px) / entry_px * 1e4)
                    last = b.last_price or last
                cur += timedelta(seconds=1)
            ret = (last - entry_px) / entry_px * 1e4
            if direction == "SHORT":
                ret = -ret
            row[key] = ret
            row[f"mfe_{h}s_bps"] = mfe
            row[f"mae_{h}s_bps"] = mae
        out.append(row)
    return out
