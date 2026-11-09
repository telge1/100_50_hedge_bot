"""Unit tests for CH test serialize/parity (no ClickHouse required)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from indicators.live_micro_lld_v2.models import LiquidityPoolV2

from research_charts.live_micro_lld_v2_ch_test.parity import compare_engine_pools_to_rows
from research_charts.live_micro_lld_v2_ch_test.serialize import pool_fingerprint, pool_to_ch_row, row_fingerprint


def _sample_pool() -> LiquidityPoolV2:
    ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    return LiquidityPoolV2(
        pool_id="lldv2:TUTUSDT:5m:normal:upper:1",
        pool_type="normal",
        symbol="TUTUSDT",
        timeframe="5m",
        side="upper",
        source_timestamp=ts,
        created_timestamp=ts,
        known_at=ts,
        top_price=1.1,
        bottom_price=1.0,
        strength=2.5,
        strength_class="Medium",
        active=False,
        break_at=datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc),
        invalidated_at=datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc),
        reclaim_level=1.05,
        created_index=42,
    )


def test_pool_roundtrip_fingerprint():
    p = _sample_pool()
    row = pool_to_ch_row(p, run_id="00000000-0000-0000-0000-000000000001")
    assert pool_fingerprint(p) == row_fingerprint(row)


def test_compare_engine_pools_to_rows_pass():
    p = _sample_pool()
    row = pool_to_ch_row(p, run_id="00000000-0000-0000-0000-000000000001")
    rep = compare_engine_pools_to_rows([p], [row])
    assert rep["fingerprint_match"] is True
    assert rep["pool_id_match"] is True
