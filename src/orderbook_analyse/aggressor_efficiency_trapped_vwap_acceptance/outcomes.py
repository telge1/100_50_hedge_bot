"""Forward outcomes — never feed features/decisions."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket
from orderbook_analyse.aggressor_efficiency_flip.timeutil import floor_second, iso_z
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import OUTCOME_HORIZONS_S


def compute_forward_outcomes(
    *,
    event_id: str,
    symbol: str,
    direction: str,
    entry_ts: datetime,
    entry_price: Optional[float],
    buckets: dict[datetime, SecondBucket],
    data_end: datetime,
    horizons: tuple[int, ...] = OUTCOME_HORIZONS_S,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": event_id,
        "symbol": symbol,
        "direction": direction,
        "outcome_entry_ts": iso_z(entry_ts),
        "outcome_entry_price": entry_price,
    }
    if entry_price is None or entry_price <= 0:
        row["outcome_status"] = "UNKNOWN_DATA"
        return row

    data_end = floor_second(data_end)
    for h in horizons:
        end_h = entry_ts + timedelta(seconds=h)
        prefix = f"h{h}s"
        if end_h > data_end:
            row[f"{prefix}_signed_return_bps"] = None
            row[f"{prefix}_MFE_bps"] = None
            row[f"{prefix}_MAE_bps"] = None
            row[f"{prefix}_available"] = False
            continue
        mfe = mae = 0.0
        last = float(entry_price)
        cur = floor_second(entry_ts)
        t_mfe = t_mae = None
        first_dir = None
        while cur < end_h:
            b = buckets.get(cur)
            if b and b.high_price is not None and b.low_price is not None:
                if direction == "LONG":
                    up = (b.high_price - entry_price) / entry_price * 1e4
                    dn = (entry_price - b.low_price) / entry_price * 1e4
                else:
                    up = (entry_price - b.low_price) / entry_price * 1e4
                    dn = (b.high_price - entry_price) / entry_price * 1e4
                if up > mfe:
                    mfe = up
                    t_mfe = cur + timedelta(seconds=1)
                if dn > mae:
                    mae = dn
                    t_mae = cur + timedelta(seconds=1)
                if first_dir is None and (up > 0.5 or dn > 0.5):
                    first_dir = "FAVORABLE" if up >= dn else "ADVERSE"
                last = b.last_price or last
            cur += timedelta(seconds=1)
        signed = (last - entry_price) / entry_price * 1e4
        if direction == "SHORT":
            signed = -signed
        row[f"{prefix}_signed_return_bps"] = signed
        row[f"{prefix}_MFE_bps"] = mfe
        row[f"{prefix}_MAE_bps"] = mae
        row[f"{prefix}_first_move_direction"] = first_dir
        row[f"{prefix}_time_to_MFE_s"] = (t_mfe - entry_ts).total_seconds() if t_mfe else None
        row[f"{prefix}_time_to_MAE_s"] = (t_mae - entry_ts).total_seconds() if t_mae else None
        row[f"{prefix}_available"] = True
    row["outcome_status"] = "OK"
    return row
