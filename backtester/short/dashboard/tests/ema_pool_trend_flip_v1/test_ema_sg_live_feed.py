from __future__ import annotations

from datetime import datetime, timezone

from ema_sg_signals.feed import _correct_legacy_local, _display_row, paginated_live_signals


def test_live_feed_paginates_and_filters():
    rows = [
        {
            "id": 3,
            "symbol": "ENAUSDT",
            "trade_direction": "SHORT",
            "signal_state": "CREATED",
            "created_on": datetime(2026, 9, 18, 7, 30),
            "candle_time": datetime(2026, 9, 18, 7, 30),
            "expected_open_price": 0.17,
            "current_price": 0.17,
            "distance": 3.9,
            "expected_distance": 3.59,
            "batchId": 4,
        },
        {
            "id": 2,
            "symbol": "BTCUSDT",
            "trade_direction": "LONG",
            "signal_state": "COMPLETED",
            "created_on": datetime(2026, 9, 17, 12, 0),
            "candle_time": datetime(2026, 9, 17, 12, 0),
            "expected_open_price": 100000,
            "current_price": 100000,
            "distance": 1.5,
            "expected_distance": 1.49,
            "batchId": 1,
        },
        {
            "id": 1,
            "symbol": "ENAUSDT",
            "trade_direction": "SHORT",
            "signal_state": "CREATED",
            "created_on": datetime(2026, 9, 18, 5, 0),
            "candle_time": datetime(2026, 9, 18, 5, 0),
            "expected_open_price": 0.16,
            "current_price": 0.16,
            "distance": 3.7,
            "expected_distance": 3.59,
            "batchId": 4,
        },
    ]

    def fetch(sql, params):
        text = " ".join(sql.split()).upper()
        if "COUNT(*) AS C FROM TRADE_SIGNALS" in text and "GROUP BY" not in text:
            filtered = _apply(rows, sql, params)
            return [{"c": len(filtered)}]
        if "GROUP BY SIGNAL_STATE" in text:
            filtered = _apply(rows, sql, params)
            counts: dict[str, int] = {}
            for row in filtered:
                counts[row["signal_state"]] = counts.get(row["signal_state"], 0) + 1
            return [{"signal_state": k, "c": v} for k, v in counts.items()]
        if "DISTINCT SYMBOL" in text:
            return [{"s": "BTCUSDT"}, {"s": "ENAUSDT"}]
        filtered = _apply(rows, sql, params)
        filtered = sorted(filtered, key=lambda r: r["candle_time"], reverse=True)
        limit = int(params[-2])
        offset = int(params[-1])
        return filtered[offset : offset + limit]

    first = paginated_live_signals(page=0, page_size=1, fetch=fetch)
    assert first["feed_ready"] is True
    assert first["pagination"]["total_filtered"] == 3
    assert first["signals"][0]["symbol"] == "ENAUSDT"
    assert first["signals"][0]["thr_label"] == "3.59%"
    assert first["signals"][0]["signal_time_label"] == "2026-09-18 07:30:00 UTC"
    assert first["page_summary"]["created"] == 2
    ena = paginated_live_signals(symbol="ENAUSDT", page_size=50, fetch=fetch)
    assert {r["signal_id"] for r in ena["signals"]} == {3, 1}
    short = paginated_live_signals(direction="SHORT", page_size=50, fetch=fetch)
    assert all(r["direction_label"] == "SHORT" for r in short["signals"])
    created = paginated_live_signals(state="CREATED", page_size=50, fetch=fetch)
    assert created["pagination"]["total_filtered"] == 2


def test_legacy_cest_candle_time_displayed_as_utc():
    """candle_time wrongly stored as CEST (+2) must render as real UTC."""
    row = {
        "id": 99,
        "symbol": "UNIUSDT",
        "trade_direction": "SHORT",
        "signal_state": "CREATED",
        "created_on": datetime(2026, 9, 18, 7, 35, 1),
        "candle_time": datetime(2026, 9, 18, 9, 35),
        "expected_open_price": 4.5,
        "current_price": 4.5,
        "distance": 3.9,
        "expected_distance": 3.5,
    }
    fixed = _correct_legacy_local(row["candle_time"], row["created_on"])
    assert fixed == datetime(2026, 9, 18, 7, 35, tzinfo=timezone.utc)
    disp = _display_row(row)
    assert disp["signal_time_label"] == "2026-09-18 07:35:00 UTC"
    assert disp["created_label"] == "2026-09-18 07:35:01 UTC"


def _apply(rows, sql, params):
    text = " ".join(sql.split()).upper()
    out = list(rows)
    values = list(params)
    if "UPPER(SYMBOL) = %S" in text:
        want = values.pop(0)
        out = [r for r in out if r["symbol"] == want]
    if "TRADE_DIRECTION = %S" in text:
        want = values.pop(0)
        out = [r for r in out if r["trade_direction"] == want]
    if "SIGNAL_STATE = %S" in text:
        want = values.pop(0)
        out = [r for r in out if r["signal_state"] == want]
    return out
