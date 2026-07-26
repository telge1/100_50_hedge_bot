"""Parser/counter unit tests for Bybit WS diagnostic (no network)."""

from __future__ import annotations

import orjson

from orderbook_analyse.bybit_ws_diagnostic import (
    DiagnosticCounters,
    classify_and_process,
    count_data_elements,
    process_raw_message,
)


def _apply(payload: dict) -> DiagnosticCounters:
    counters = DiagnosticCounters()
    classify_and_process(payload, counters)
    return counters


def test_count_data_elements() -> None:
    assert count_data_elements([1, 2, 3]) == 3
    assert count_data_elements({"a": 1}) == 1
    assert count_data_elements(None) == 0
    assert count_data_elements("x") == 0


def test_orderbook_snapshot() -> None:
    payload = {
        "topic": "orderbook.200.APTUSDT",
        "type": "snapshot",
        "ts": 1672304484978,
        "data": {
            "s": "APTUSDT",
            "b": [["5.10", "100"], ["5.09", "50"]],
            "a": [["5.11", "80"], ["5.12", "20"], ["5.13", "10"]],
            "u": 100,
            "seq": 1000,
        },
    }
    counters = _apply(payload)
    ob = counters.orderbook
    assert ob.message_count == 1
    assert ob.data_element_count == 1
    assert ob.snapshot_count == 1
    assert ob.delta_count == 0
    assert ob.bid_level_count == 2
    assert ob.ask_level_count == 3
    assert ob.min_u == 100
    assert ob.max_u == 100
    assert ob.min_seq == 1000
    assert ob.max_seq == 1000
    assert ob.first_message_at is not None
    assert ob.last_message_at == ob.first_message_at


def test_orderbook_delta() -> None:
    counters = DiagnosticCounters()
    classify_and_process(
        {
            "topic": "orderbook.200.APTUSDT",
            "type": "snapshot",
            "data": {"s": "APTUSDT", "b": [["5.10", "1"]], "a": [["5.11", "1"]], "u": 10, "seq": 50},
        },
        counters,
    )
    classify_and_process(
        {
            "topic": "orderbook.200.APTUSDT",
            "type": "delta",
            "data": {
                "s": "APTUSDT",
                "b": [["5.10", "0"]],
                "a": [["5.12", "2"], ["5.13", "3"]],
                "u": 12,
                "seq": 55,
            },
        },
        counters,
    )
    ob = counters.orderbook
    assert ob.message_count == 2
    assert ob.snapshot_count == 1
    assert ob.delta_count == 1
    assert ob.bid_level_count == 2
    assert ob.ask_level_count == 3
    assert ob.min_u == 10
    assert ob.max_u == 12
    assert ob.min_seq == 50
    assert ob.max_seq == 55


def test_public_trade() -> None:
    payload = {
        "topic": "publicTrade.APTUSDT",
        "type": "snapshot",
        "ts": 1672304486868,
        "data": [
            {
                "T": 1672304486865,
                "s": "APTUSDT",
                "S": "Buy",
                "v": "1.5",
                "p": "5.10",
                "i": "abc",
            },
            {
                "T": 1672304486866,
                "s": "APTUSDT",
                "S": "Sell",
                "v": "0.2",
                "p": "5.09",
                "i": "def",
            },
            {
                "T": 1672304486867,
                "s": "APTUSDT",
                "S": "Buy",
                "v": "0.1",
                "p": "5.10",
                "i": "ghi",
            },
        ],
    }
    counters = _apply(payload)
    tr = counters.public_trade
    assert tr.message_count == 1
    assert tr.data_element_count == 3
    assert tr.trade_count == 3
    assert tr.buy_count == 2
    assert tr.sell_count == 1


def test_ticker_delta_partial_fields() -> None:
    payload = {
        "topic": "tickers.APTUSDT",
        "type": "delta",
        "cs": 1,
        "ts": 1,
        "data": {
            "symbol": "APTUSDT",
            "lastPrice": "5.10",
            "openInterest": "12345",
            "bid1Price": "5.09",
        },
    }
    counters = _apply(payload)
    tk = counters.ticker
    assert tk.message_count == 1
    assert tk.data_element_count == 1
    assert tk.fields_seen == {"lastPrice", "openInterest", "bid1Price"}
    fields = tk.to_dict()["fields_seen"]
    assert fields["lastPrice"] is True
    assert fields["openInterest"] is True
    assert fields["markPrice"] is False
    assert fields["fundingRate"] is False


def test_liquidation() -> None:
    payload = {
        "topic": "allLiquidation.APTUSDT",
        "type": "snapshot",
        "ts": 1739502303204,
        "data": [
            {
                "T": 1739502302929,
                "s": "APTUSDT",
                "S": "Sell",
                "v": "200",
                "p": "5.00",
            },
            {
                "T": 1739502302930,
                "s": "APTUSDT",
                "S": "Buy",
                "v": "50",
                "p": "5.20",
            },
        ],
    }
    counters = _apply(payload)
    liq = counters.liquidation
    assert liq.message_count == 1
    assert liq.data_element_count == 2
    assert liq.event_count == 2
    assert liq.buy_count == 1
    assert liq.sell_count == 1


def test_subscription_ack_and_pong() -> None:
    counters = DiagnosticCounters()
    classify_and_process(
        {"success": True, "ret_msg": "subscribe", "op": "subscribe", "conn_id": "x"},
        counters,
    )
    classify_and_process({"op": "pong", "args": ["1760000000000"], "conn_id": "x"}, counters)
    classify_and_process(
        {"success": True, "ret_msg": "pong", "op": "ping", "conn_id": "x"},
        counters,
    )
    assert counters.subscription_ack_count == 1
    assert counters.pong_count == 2
    assert counters.orderbook.message_count == 0


def test_process_raw_message_orjson() -> None:
    counters = DiagnosticCounters()
    raw = orjson.dumps(
        {
            "topic": "publicTrade.APTUSDT",
            "type": "snapshot",
            "data": [{"S": "Buy", "v": "1", "p": "1", "s": "APTUSDT", "T": 1, "i": "1"}],
        }
    )
    process_raw_message(raw, counters)
    assert counters.public_trade.trade_count == 1
    assert counters.parse_error_count == 0

    process_raw_message(b"{not-json", counters)
    assert counters.parse_error_count == 1
