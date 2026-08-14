from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from pool_order_plan_v1 import store
from pool_order_plan_v1.batch import BatchAbort, progress, run_batch
from pool_order_plan_v1.candles import causal_prefix, five_minute_from_csv
from pool_order_plan_v1.config import WARMUP_DAYS
from pool_order_plan_v1.planner_client import call_plan_orders
from pool_order_plan_v1.pool_snapshot import (
    causal_as_of,
    plan_from_snapshot,
    plan_parity_core,
    pool_engine_run_count,
    reset_pool_engine_run_count,
    run_pools_once,
    snapshot_pools,
    structural_pool_keys,
)
from pool_order_plan_v1.schema import clickhouse_candle_stamp
from pool_order_plan_v1.store import SourceRejected


CSV = Path("/home/telgenbuescher/projects/pool_order_planer/data/HYPEUSDT_5m_2026-08-01_2026-08-12.csv")
UTC = timezone.utc


def _expand_1m(series) -> list[dict]:
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
    return rows_1m


def _hype_cases(n: int = 10) -> list[tuple[str, float, str]]:
    series = five_minute_from_csv(CSV)
    df = series.bars
    # skip early bars so lookback/percentile have room
    picks = df.iloc[400:-50: max(1, (len(df) - 450) // n)].head(n)
    cases = []
    for _, row in picks.iterrows():
        open_t = pd.Timestamp(row["timestamp"]).tz_convert("UTC")
        entry = (open_t + pd.Timedelta(minutes=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cases.append((entry, float(row["close"]), "LONG" if len(cases) % 2 == 0 else "SHORT"))
    assert len(cases) >= n
    return cases[:n]


def test_progress_output_flushes(capsys):
    progress("[HYPEUSDT] running pool engine once")
    assert "[HYPEUSDT] running pool engine once" in capsys.readouterr().out


def test_warmup_is_14_days_before_earliest(monkeypatch):
    captured = {}

    def fake_load(symbol, *, start=None, end=None):
        captured["start"] = start
        captured["end"] = end
        raise RuntimeError("stop-after-window")

    monkeypatch.setattr("pool_order_plan_v1.batch.load_closed_1m", fake_load)
    earliest = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    later = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
    with pytest.raises(BatchAbort, match="CSV fallback is forbidden"):
        run_batch(
            signals=[
                {
                    "signal_id": "a",
                    "symbol": "HYPEUSDT",
                    "direction": "LONG",
                    "timeframe": "15m",
                    "entry_time": later,
                    "entry_price": 54.0,
                    "available_at": later,
                    "created_at": later,
                },
                {
                    "signal_id": "b",
                    "symbol": "HYPEUSDT",
                    "direction": "LONG",
                    "timeframe": "15m",
                    "entry_time": earliest,
                    "entry_price": 54.0,
                    "available_at": earliest,
                    "created_at": earliest,
                },
            ],
            skip_pin=True,
            publish=False,
        )
    assert captured["start"] == earliest - timedelta(days=WARMUP_DAYS)
    assert WARMUP_DAYS == 14


def test_signals_processed_chronologically(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    series = five_minute_from_csv(CSV)
    rows = _expand_1m(series)
    t1 = "2026-08-11T01:17:00Z"
    t0 = "2026-08-10T16:55:00Z"
    result = run_batch(
        signals=[
            {
                "signal_id": "later",
                "symbol": "HYPEUSDT",
                "direction": "LONG",
                "timeframe": "15m",
                "entry_time": t1,
                "entry_price": 54.91,
                "available_at": t1,
                "created_at": t1,
            },
            {
                "signal_id": "earlier",
                "symbol": "HYPEUSDT",
                "direction": "SHORT",
                "timeframe": "15m",
                "entry_time": t0,
                "entry_price": 54.04,
                "available_at": t0,
                "created_at": t0,
            },
        ],
        one_minute_by_symbol={"HYPEUSDT": rows},
        skip_pin=True,
        publish=False,
    )
    assert result["manifest"]["counts"]["winners"] == 2
    pre = (tmp_path / result["run_id"] / "preflight.json").read_text()
    assert pre.find("earlier") < pre.find("later")


def test_pool_engine_once_not_per_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    series = five_minute_from_csv(CSV)
    rows = _expand_1m(series)
    cases = _hype_cases(20)
    sigs = []
    for i, (ts, px, d) in enumerate(cases):
        sigs.append(
            {
                "signal_id": f"s{i}",
                "symbol": "HYPEUSDT",
                "direction": d,
                "timeframe": "15m",
                "entry_time": ts,
                "entry_price": px,
                "available_at": ts,
                "created_at": ts,
            }
        )
    reset_pool_engine_run_count()
    before = pool_engine_run_count()
    result = run_batch(
        signals=sigs,
        one_minute_by_symbol={"HYPEUSDT": rows},
        skip_pin=True,
        publish=False,
    )
    after = pool_engine_run_count()
    assert after - before == 1
    assert result["manifest"]["counts"]["pool_engine_runs"] == 1
    assert result["manifest"]["counts"]["winners"] == 20
    assert result["manifest"]["pool_engine_runs_per_symbol"] == 1


def test_later_invalidation_stays_in_earlier_snapshot():
    from research.liquidity.bigbeluga_pools import LiquidityPool, Side

    early = pd.Timestamp("2026-08-10T16:50:00Z")
    late = pd.Timestamp("2026-08-11T01:10:00Z")
    pool = LiquidityPool(
        pool_id=1,
        lookback=8,
        side=Side.UPPER,
        source_bar_time=early - pd.Timedelta(minutes=5),
        created_at=early,
        source_high=1.0,
        source_low=0.9,
        source_range=0.1,
        top=1.1,
        bottom=1.0,
        mid=1.05,
        strength=2.0,
        active=False,
        invalidated_at=late,
    )
    snap = snapshot_pools([pool], early)
    assert len(snap) == 1
    later_snap = snapshot_pools([pool], late)
    assert later_snap == []


def test_later_candle_does_not_change_earlier_snapshot():
    series = five_minute_from_csv(CSV)
    entry = "2026-08-10T16:55:00Z"
    prefix = causal_prefix(series, entry)
    as_of = causal_as_of(prefix)
    pools_prefix = run_pools_once(prefix)
    pools_full = run_pools_once(series.bars)
    a = plan_parity_core(
        plan_from_snapshot(
            pools_prefix,
            symbol="HYPEUSDT",
            entry_time=entry,
            entry_price=54.04,
            direction="LONG",
            as_of=as_of,
            test_fixture_only=True,
        )
    )
    b = plan_parity_core(
        plan_from_snapshot(
            pools_full,
            symbol="HYPEUSDT",
            entry_time=entry,
            entry_price=54.04,
            direction="LONG",
            as_of=as_of,
            test_fixture_only=True,
        )
    )
    assert a == b


def test_single_pass_matches_ten_hype_reference_frames():
    series = five_minute_from_csv(CSV)
    reset_pool_engine_run_count()
    pools = run_pools_once(series.bars)
    mismatches = []
    for ts, px, direction in _hype_cases(10):
        prefix = causal_prefix(series, ts)
        ref = call_plan_orders(
            prefix,
            symbol="HYPEUSDT",
            entry_time=ts,
            entry_price=px,
            direction=direction,
            test_fixture_only=True,
        )
        opt = plan_from_snapshot(
            pools,
            symbol="HYPEUSDT",
            entry_time=ts,
            entry_price=px,
            direction=direction,
            as_of=causal_as_of(prefix),
            test_fixture_only=True,
        )
        if plan_parity_core(ref) != plan_parity_core(opt):
            mismatches.append(ts)
        assert structural_pool_keys(ref) == structural_pool_keys(opt)
    assert mismatches == []


def test_ctrl_c_does_not_update_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    good = store.write_run(
        "keep-latest",
        manifest=clickhouse_candle_stamp() | {"ok": True},
        preflight={},
        coverage={},
        plans=[],
        outcomes=[{"signal_id": "keep", "tp1_price": 2.0}],
        ignored=[],
    )
    store.publish_latest(good)
    series = five_minute_from_csv(CSV)
    rows = _expand_1m(series)

    def boom(*_a, **_k):
        raise KeyboardInterrupt()

    monkeypatch.setattr("pool_order_plan_v1.batch.plan_from_snapshot", boom)
    with pytest.raises(BatchAbort, match="interrupted"):
        run_batch(
            signals=[
                {
                    "signal_id": "x",
                    "symbol": "HYPEUSDT",
                    "direction": "LONG",
                    "timeframe": "15m",
                    "entry_time": "2026-08-11T01:17:00Z",
                    "entry_price": 54.91,
                    "available_at": "2026-08-11T01:15:00Z",
                    "created_at": "2026-08-11T01:15:00Z",
                }
            ],
            one_minute_by_symbol={"HYPEUSDT": rows},
            skip_pin=True,
            publish=False,
        )
    assert (tmp_path / "latest").resolve().name == "keep-latest"
    aborted = list(tmp_path.glob("*/ABORTED.json"))
    assert aborted
    with pytest.raises(SourceRejected):
        store.publish_latest(aborted[0].parent)


def test_single_pass_batch_progress(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    series = five_minute_from_csv(CSV)
    rows = _expand_1m(series)
    run_batch(
        signals=[
            {
                "signal_id": "sig-1",
                "symbol": "HYPEUSDT",
                "direction": "LONG",
                "timeframe": "15m",
                "entry_time": "2026-08-11T01:17:00Z",
                "entry_price": 54.91,
                "available_at": "2026-08-11T01:15:00Z",
                "created_at": "2026-08-11T01:15:00Z",
            }
        ],
        one_minute_by_symbol={"HYPEUSDT": rows},
        skip_pin=True,
        publish=False,
    )
    out = capsys.readouterr().out
    assert "[HYPEUSDT] aggregating 1m -> 5m" in out
    assert "[HYPEUSDT] running pool engine once" in out
    assert "[HYPEUSDT 1/1]" in out
