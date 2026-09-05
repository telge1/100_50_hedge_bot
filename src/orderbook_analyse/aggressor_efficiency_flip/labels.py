"""OI / availability labels (optional)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from orderbook_analyse.aggressor_efficiency_flip.timeutil import floor_second


def classify_oi_price(
    *,
    price_start: Optional[float],
    price_end: Optional[float],
    oi_tag: Optional[str],
) -> str:
    if oi_tag is None or oi_tag == "MISSING":
        return "MISSING"
    if price_start is None or price_end is None or price_start <= 0:
        return "MIXED"
    up = price_end > price_start
    down = price_end < price_start
    oi_up = oi_tag == "OI_UP"
    oi_down = oi_tag == "OI_DOWN"
    if up and oi_down:
        return "PRICE_UP_OI_DOWN"
    if up and oi_up:
        return "PRICE_UP_OI_UP"
    if down and oi_down:
        return "PRICE_DOWN_OI_DOWN"
    if down and oi_up:
        return "PRICE_DOWN_OI_UP"
    if not up and not down:
        return "FLAT"
    return "MIXED"


def attach_oi_class(
    candidate: dict,
    oi_labels: dict[datetime, str],
) -> None:
    from datetime import timedelta

    from orderbook_analyse.aggressor_efficiency_flip.timeutil import parse_utc

    final = candidate.get("final_decision_ts")
    if not final:
        candidate["oi_class"] = "MISSING"
        candidate["oi_available"] = False
        return
    ts = floor_second(parse_utc(final))
    # OI is 5s-aligned; no future buckets — floor to prior/equal 5s grid.
    aligned = ts - timedelta(seconds=ts.second % 5)
    tag = oi_labels.get(aligned)
    if tag is None:
        for i in range(1, 12):
            tag = oi_labels.get(aligned - timedelta(seconds=5 * i))
            if tag:
                break
    px0 = candidate.get("compression_end_price")
    price_end = candidate.get("diagnostic_earliest_entry_price") or candidate.get(
        "compression_end_price"
    )
    candidate["oi_class"] = classify_oi_price(
        price_start=float(px0) if px0 is not None else None,
        price_end=float(price_end) if price_end is not None else None,
        oi_tag=tag or "MISSING",
    )
    # Availability = OI series present at/near decision, independent of price class.
    candidate["oi_available"] = tag is not None
    if tag is not None and candidate["oi_class"] == "MISSING":
        # price missing but OI present → still label via OI tag only
        candidate["oi_class"] = "MIXED" if tag != "OI_FLAT" else "FLAT"
