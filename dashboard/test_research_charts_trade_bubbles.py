"""Unit tests for causal public-trade bubble aggregation (no CH, no execution)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from research_charts.trade_bubbles import aggregate, classify_size


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _t(tid: str, ts: str, side: str, price: float, size: float) -> dict:
    t = _ts(ts)
    return {
        "trade_id": tid,
        "trade_ts": t,
        "side": side,
        "price": price,
        "size": size,
        "notional": price * size,
        "source": "test",
        "received_at": t,
    }


def test_aggressor_buy_sell_buckets():
    trades = [
        _t("1", "2026-08-28T06:35:00.100Z", "Buy", 0.08817, 1000),
        _t("2", "2026-08-28T06:35:00.200Z", "Sell", 0.08817, 400),
    ]
    bubbles = aggregate(trades, symbol="DOGEUSDT", as_of=_ts("2026-08-28T06:35:05Z"), mode="all")
    assert len(bubbles) == 1
    b = bubbles[0]
    assert b["dominant_side"] == "BUY"
    assert b["buy_notional"] > b["sell_notional"]
    assert b["known_at"].endswith("Z")
    assert b["forming"] is False
    assert b["research_only"] is True


def test_cursor_excludes_future_and_dedupe_via_aggregate_input():
    trades = [
        _t("1", "2026-08-28T06:35:00Z", "Buy", 0.08, 10),
        _t("2", "2026-08-28T06:40:00Z", "Sell", 0.08, 10),
    ]
    as_of = _ts("2026-08-28T06:36:00Z")
    bubbles = aggregate(trades, symbol="DOGEUSDT", as_of=as_of, mode="all")
    assert all(b["timestamp"] <= int(as_of.timestamp()) for b in bubbles)


def test_size_warmup_uncalibrated():
    assert classify_size(100.0, [1.0] * 10)[0] == "UNCALIBRATED"


def test_forming_marked():
    trades = [_t("a", "2026-08-28T06:35:00.100Z", "Buy", 0.08817, 500)]
    as_of = _ts("2026-08-28T06:35:00.500Z")
    bubbles = aggregate(trades, symbol="DOGEUSDT", as_of=as_of, mode="all")
    assert bubbles and bubbles[0]["forming"] is True
    assert bubbles[0]["known_at"] == as_of.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def test_prefix_parity_simple():
    base = _ts("2026-08-28T06:35:00Z")
    trades = []
    for i in range(20):
        ts = base + timedelta(seconds=i)
        trades.append(
            _t(
                str(i),
                ts.isoformat().replace("+00:00", "Z"),
                "Buy" if i % 2 == 0 else "Sell",
                0.088,
                50 + i,
            )
        )
    as_of = base + timedelta(seconds=10)
    full = aggregate(trades, symbol="DOGEUSDT", as_of=as_of, mode="all")
    pref = aggregate(
        [t for t in trades if t["trade_ts"] <= as_of],
        symbol="DOGEUSDT",
        as_of=as_of,
        mode="all",
    )
    assert full == pref


def test_no_execution_imports():
    import research_charts.trade_bubbles as m

    src = open(m.__file__, encoding="utf-8").read()
    assert "pybit" not in src.lower()
    assert "order_create" not in src
    assert "execute" not in src.lower() or "max_execution_time" in src
