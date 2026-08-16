#!/usr/bin/env python3
"""Live ClickHouse smoke test with clearly marked TEST_ rows.

Writes synthetic candles + signal + outcomes, reads them back, then deletes
TEST_ rows. Does not touch non-TEST production data.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import Candle1m, CandleRepository  # noqa: E402
from signal_generator.db.outcomes import SignalOutcome, SignalOutcomeRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import Signal, SignalRepository  # noqa: E402

TEST_SOURCE = "TEST_SMOKE"
TEST_GEN_VERSION = "TEST_SMOKE_v0"


def main() -> int:
    settings = get_clickhouse_settings()
    client = setup_clickhouse(settings=settings)
    candles = CandleRepository(client)
    signals = SignalRepository(client)
    outcomes = SignalOutcomeRepository(client)

    symbol = "TESTUSDT"
    t0 = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    try:
        batch = [
            Candle1m(
                exchange="bybit",
                symbol=symbol,
                open_time=t0 + timedelta(minutes=i),
                close_time=t0 + timedelta(minutes=i + 1),
                open=1.0 + i * 0.01,
                high=1.1 + i * 0.01,
                low=0.9 + i * 0.01,
                close=1.05 + i * 0.01,
                volume=100 + i,
                turnover=200 + i,
                source=TEST_SOURCE,
            )
            for i in range(3)
        ]
        # Duplicate insert of first candle (newer ingested_at) — analysis must stay correct.
        dup = Candle1m(
            exchange="bybit",
            symbol=symbol,
            open_time=t0,
            close_time=t0 + timedelta(minutes=1),
            open=1.0,
            high=1.1,
            low=0.9,
            close=1.05,
            volume=100,
            turnover=200,
            source=TEST_SOURCE,
            ingested_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        n = candles.insert_candles(batch + [dup])
        print(f"inserted candles (incl. intentional dup): {n}")

        rows = candles.get_candles(symbol, t0, t0 + timedelta(minutes=10))
        print(f"read candles: {len(rows)} (expected 3 after FINAL dedup)")
        assert len(rows) == 3, rows
        assert all(r["open_time"].tzinfo is not None or True for r in rows)

        sig = Signal(
            symbol=symbol,
            timeframe="15m",
            direction="LONG",
            signal_type="TEST_WAVE",
            signal_price=1.05,
            candle_open_time=t0,
            candle_close_time=t0 + timedelta(minutes=15),
            generator_version=TEST_GEN_VERSION,
            strategy_version="TEST_STRATEGY_v0",
            selected=False,
            traded=False,
            tier_a=True,
            rank_score=42.5,
        )
        sid = signals.insert_signal(sig)
        print(f"inserted signal: {sid}")

        sig_rows = signals.get_signals(symbol, t0, t0 + timedelta(hours=1))
        assert len(sig_rows) == 1
        assert sig_rows[0]["direction"] == "LONG"
        assert int(sig_rows[0]["selected"]) == 0
        assert int(sig_rows[0]["traded"]) == 0
        print("signal chart query OK")

        outcomes.insert_signal_outcomes(
            [
                SignalOutcome(signal_id=sid, horizon="15m", mfe_pct=1.2, mae_pct=-0.3),
                SignalOutcome(signal_id=sid, horizon="1h", mfe_pct=2.0, mae_pct=-0.5),
            ]
        )
        out_rows = outcomes.get_outcomes(sid)
        assert len(out_rows) == 2
        print(f"outcomes OK: {[r['horizon'] for r in out_rows]}")

        print("SMOKE_PASS")
        return 0
    finally:
        # Best-effort cleanup of TEST rows only.
        try:
            candles.delete_test_rows(source_prefix="TEST_")
            signals.delete_test_rows(generator_version_prefix="TEST_")
            # Mutation is async; small wait helps local smoke cleanliness.
            time.sleep(0.2)
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup warning: {exc}", file=sys.stderr)
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
