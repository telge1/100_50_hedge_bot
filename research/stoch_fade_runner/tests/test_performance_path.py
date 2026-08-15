from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from research.stoch_fade_runner.candles import MemoryCandleSource
from research.stoch_fade_runner.config import CANARY_SYMBOL, ensure_sg_on_path
from research.stoch_fade_runner.engine import evaluate_symbol
from research.stoch_fade_runner.htf import aggregate_1m_to_timeframe as grouped_agg
from research.stoch_fade_runner.stages import StageRecorder


def _bars(n: int, start: datetime):
    ensure_sg_on_path()
    from signal_generator.timeframes import OhlcvBar

    out = []
    px = 1.0
    for i in range(n):
        ot = start + timedelta(minutes=i)
        ct = ot + timedelta(minutes=1)
        out.append(
            OhlcvBar(
                open_time=ot,
                close_time=ct,
                open=px,
                high=px + 0.01,
                low=px - 0.01,
                close=px,
                volume=1.0,
                turnover=0.0,
            )
        )
    return out


def test_grouped_agg_matches_sg_inspect_path() -> None:
    ensure_sg_on_path()
    from signal_generator.timeframes import aggregate_1m_to_timeframe as sg_agg

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = _bars(240, start)
    as_of = bars[-1].close_time
    for tf in ("15m", "30m", "1h"):
        slow = sg_agg(bars, tf, as_of=as_of, require_complete=True)
        fast = grouped_agg(bars, tf, as_of=as_of, require_complete=True)
        assert len(slow) == len(fast)
        for a, b in zip(slow, fast, strict=True):
            assert a.open_time == b.open_time
            assert a.close_time == b.close_time
            assert a.open == b.open
            assert a.high == b.high
            assert a.low == b.low
            assert a.close == b.close


def test_engine_calls_frozen_kernels_once_per_tf(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    rows = []
    t0 = start - timedelta(days=1)
    for i in range(60 * 30):
        ot = t0 + timedelta(minutes=i)
        px = 100.0 + (i % 20) * 0.1
        rows.append(
            {
                "open_time": ot,
                "open": px,
                "high": px + 0.2,
                "low": px - 0.2,
                "close": px + 0.05,
                "volume": 1.0,
            }
        )
    src = MemoryCandleSource({CANARY_SYMBOL: pd.DataFrame(rows)})
    counts: dict[str, int] = {}
    rec = StageRecorder(tmp_path, run_id="t")
    out = evaluate_symbol(
        symbol=CANARY_SYMBOL,
        candle_source=src,
        signal_start=start,
        signal_end_exclusive=end,
        recorder=rec,
        call_counts=counts,
    )
    assert (tmp_path / "status.json").is_file()
    assert (tmp_path / "run.log").is_file()
    assert counts["aggregate_1m_to_timeframe"] == 4
    assert counts["build_waves_from_ohlcv"] == 4
    assert counts["build_symbol_signals"] == 1
    assert counts.get("attach_resolved_entries", 0) <= 4
    engine = Path(__file__).resolve().parents[1] / "engine.py"
    text = engine.read_text(encoding="utf-8")
    assert "for _, row in raw.iterrows()" not in text
    assert out["status"] in {"EVALUATED_WITH_SIGNALS", "EVALUATED_NO_SIGNAL"}
    names = [s["name"] for s in rec.stages]
    assert "candle_normalization" in names
    assert "aggregate_15m" in names
    assert rec.status in {"COMPLETED", "FAILED"}
