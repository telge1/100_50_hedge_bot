"""Integration tests: CH test chart API vs engine (read-only CH)."""

from __future__ import annotations

import sys
from pathlib import Path

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from research_charts.chart_live_micro_lld_v2 import chart_live_micro_lld_v2
from research_charts.chart_live_micro_lld_v2_ch_test import (
    DEFAULT_RUN_ID,
    chart_live_micro_lld_v2_ch_test,
)
from research_charts.live_micro_lld_v2_ch_test.errors import OutOfCoverageError
from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

FROM_CLOSE = "2026-09-08T11:20:00+00:00"
TO_CLOSE = "2026-10-08T11:20:00+00:00"


def _zone_sig(z: dict) -> tuple:
    return (
        z["pool_id"],
        round(float(z["bottom_price"]), 8),
        round(float(z["top_price"]), 8),
        int(z["start_timestamp"]),
        int(z["end_timestamp"]),
        str(z.get("strength_class") or ""),
    )


def test_ch_api_full_window_parity_with_engine():
    eng = chart_live_micro_lld_v2(
        "TUTUSDT",
        FROM_CLOSE,
        TO_CLOSE,
        timeframe="5m",
        pool_detail=10,
    )
    ch = chart_live_micro_lld_v2_ch_test(
        "TUTUSDT",
        FROM_CLOSE,
        TO_CLOSE,
        timeframe="5m",
        pool_detail=10,
        run_id=DEFAULT_RUN_ID,
    )
    eng_sigs = sorted(_zone_sig(z) for z in eng["zones"])
    ch_sigs = sorted(_zone_sig(z) for z in ch["zones"])
    assert eng_sigs == ch_sigs
    assert ch["namespace"] == "LIVE_MICRO_LLD_V2_CH_TEST"
    assert ch["run_id"] == DEFAULT_RUN_ID


def test_ch_api_out_of_coverage():
    try:
        chart_live_micro_lld_v2_ch_test(
            "TUTUSDT",
            "2025-01-01T00:00:00+00:00",
            "2025-01-02T00:00:00+00:00",
            timeframe="5m",
            pool_detail=10,
            run_id=DEFAULT_RUN_ID,
        )
        assert False, "expected OutOfCoverageError"
    except OutOfCoverageError as exc:
        assert exc.details["run_id"]
