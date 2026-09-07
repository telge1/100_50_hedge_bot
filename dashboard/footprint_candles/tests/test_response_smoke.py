"""Read-only BTC smoke for AVR timings (skipped if CH unreachable)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))


def _client_or_skip():
    try:
        from research_charts.clickhouse_config import load_clickhouse_config
        import clickhouse_connect

        cfg = load_clickhouse_config()
        client = clickhouse_connect.get_client(**cfg.connect_kwargs())
        client.query("SELECT 1")
        return client
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"ClickHouse unavailable: {exc}")


@pytest.mark.parametrize("span_s,label", [(300, "5m"), (3600, "1h"), (21600, "6h")])
def test_avr_smoke_timings_btc(span_s, label):
    from footprint_candles.service import load_footprint

    client = _client_or_skip()
    try:
        # Align to recent closed 5m boundary
        now = int(time.time())
        end = (now // 300) * 300
        start = end - span_s
        t0 = time.perf_counter()
        payload = load_footprint(
            symbol="BTCUSDT",
            timeframe="5m",
            mode="DISPLAY",
            bucket_step=5.0,
            start=start,
            end=end,
            client=client,
            include_avr=True,
        )
        elapsed = time.perf_counter() - t0
        assert payload["success"] is True
        avr = payload.get("avr_engine") or {}
        assert avr.get("enabled") is True
        # Soft budget: 6h should stay under query timeout envelope
        assert elapsed < 45.0, f"{label} too slow: {elapsed:.2f}s"
        assert avr.get("avr_payload_bytes", 0) < 2_000_000
        candles = payload.get("candles") or []
        with_avr = [c for c in candles if c.get("avr")]
        assert with_avr, "expected avr on candles"
        # Collect interesting states if present
        states = {
            (c["avr"].get("final_state"), c["avr"].get("dominant_state"))
            for c in with_avr
        }
        print(
            f"SMOKE {label}: {elapsed:.2f}s query={avr.get('timing')} "
            f"candles={len(candles)} avr_bytes={avr.get('avr_payload_bytes')} states={states}"
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
