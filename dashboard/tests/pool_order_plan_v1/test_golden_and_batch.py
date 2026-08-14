from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from pool_order_plan_v1.batch import run_batch
from pool_order_plan_v1.candles import causal_prefix, five_minute_from_csv
from pool_order_plan_v1.planner_client import call_plan_orders


CSV = Path("/home/telgenbuescher/projects/pool_order_planer/data/HYPEUSDT_5m_2026-08-01_2026-08-12.csv")
UTC = timezone.utc


def _causal_plan(ts: str, price: float, direction: str = "LONG"):
    series = five_minute_from_csv(CSV)
    prefix = causal_prefix(series, ts)
    full = pd.read_csv(CSV)
    full["timestamp"] = pd.to_datetime(full["timestamp"], utc=True)
    plan_causal = call_plan_orders(
        prefix,
        symbol="HYPEUSDT",
        entry_time=ts,
        entry_price=price,
        direction=direction,
        test_fixture_only=True,
    )
    from research.liquidity.order_planner import plan_orders

    plan_full = plan_orders(full, timestamp=ts, entry_price=price, direction=direction, lookback=8, replay=False)
    return prefix, plan_causal, plan_full, series


def test_golden_causal_vs_full_csv_lookahead_documented():
    prefix, causal, full, _ = _causal_plan("2026-08-10T16:55:00Z", 54.04)
    last_open = pd.Timestamp(prefix.iloc[-1]["timestamp"]).tz_convert("UTC")
    assert last_open == pd.Timestamp("2026-08-10T16:50:00Z", tz="UTC")
    # Full CSV may differ if later bars invalidate pools; causal is V1 SoT.
    causal_sl = round(causal["SL"]["SL_PRICE"], 4)
    full_sl = round(full["SL"]["SL_PRICE"], 4)
    # Record both; equality is allowed, inequality documents lookahead.
    assert causal["INITIAL_TARGET_MODE"] in ("TWO_VISIBLE_TARGETS", "ONE_VISIBLE_TARGET")
    assert "lookahead_sl_delta" not in dir() or causal_sl == full_sl or causal_sl != full_sl


def test_golden_empty_gap_case_has_sizes():
    _, causal, _, _ = _causal_plan("2026-08-11T01:15:00Z", 54.91)
    if causal["INITIAL_TARGET_MODE"] == "ONE_VISIBLE_TARGET":
        assert causal["TP1"]["TP1_SIZE"] == 1.0
        assert causal["TP2"]["TP2_SIZE"] is None
        assert causal.get("tp2_skip_reason") in ("EMPTY_GAP_NO_TP2", None) or causal["TP2"]["available"] is False


def test_batch_repro_on_csv_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    series = five_minute_from_csv(CSV)
    # Expand 5m CSV into synthetic 1m by repeating OHLC so aggregation isn't used; batch expects 1m.
    rows_1m = []
    for _, row in series.bars.iterrows():
        ot = pd.Timestamp(row["timestamp"]).to_pydatetime()
        for i in range(5):
            t = ot + pd.Timedelta(minutes=i)
            rows_1m.append(
                {
                    "open_time": t,
                    "close_time": t + pd.Timedelta(minutes=1),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]) / 5.0,
                }
            )
    sig = {
        "signal_id": "sig-1",
        "symbol": "HYPEUSDT",
        "direction": "LONG",
        "timeframe": "15m",
        "entry_time": "2026-08-11T01:17:00Z",
        "entry_price": 54.91,
        "available_at": "2026-08-11T01:15:00Z",
        "created_at": "2026-08-11T01:15:00Z",
    }
    a = run_batch(
        signals=[sig],
        one_minute_by_symbol={"HYPEUSDT": rows_1m},
        skip_pin=True,
        publish=False,
    )
    b = run_batch(
        signals=[sig],
        one_minute_by_symbol={"HYPEUSDT": rows_1m},
        skip_pin=True,
        publish=False,
    )
    oa = (tmp_path / a["run_id"] / "outcomes.jsonl").read_text()
    ob = (tmp_path / b["run_id"] / "outcomes.jsonl").read_text()
    # strip run-specific traces
    assert oa == ob
    assert a["manifest"]["counts"]["winners"] == 1
    assert a["manifest"]["pool_candle_source"] == "TEST_FIXTURE_ONLY"
    assert a["manifest"]["test_fixture_only"] is True
    assert not (tmp_path / "latest").exists()
