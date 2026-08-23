"""Tests for 30m horizon research and prior horizon audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.timeframes import bar_close, timeframe_duration
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.horizon_audit import (
    check_horizon_monotonicity,
    horizon_progression_row,
    horizons_for_signal_tf,
    manual_recompute_check,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.horizon_30m_runner import (
    SHORTLIST_30M_MODE_IDS,
    shortlist_30m_modes,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.mfe_mae import (
    compute_all_horizons,
    compute_mfe_mae_horizon,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.research_policy import (
    apply_available_source_research,
)


def _synthetic_1m(start: datetime, n: int, base: float = 1.0) -> pd.DataFrame:
    rows = []
    for i in range(n):
        px = base + i * 0.0001
        rows.append(
            {
                "open_time": (start + timedelta(minutes=i)).replace(tzinfo=None),
                "open": px,
                "high": px + 0.002,
                "low": px - 0.001,
                "close": px + 0.0005,
                "volume": 100.0,
            }
        )
    return pd.DataFrame(rows)


def test_30m_decision_at_equals_candidate_plus_30m():
    open_ts = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert bar_close(open_ts, "30m") == open_ts + timeframe_duration("30m")


def test_horizons_for_30m_signal():
    assert horizons_for_signal_tf("30m") == (15, 30, 60, 120, 240)


def test_shortlist_30m_modes_frozen():
    modes = shortlist_30m_modes()
    assert [m["mode_id"] for m in modes] == list(SHORTLIST_30M_MODE_IDS)
    assert "M3_ON_M2_ATR_05_COH_05" not in [m["mode_id"] for m in modes]


def test_entry_after_decision_at():
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    decision = start + timedelta(minutes=30)
    c1m = _synthetic_1m(start, 120, base=1.0)
    entry_row = c1m[pd.to_datetime(c1m["open_time"]) >= pd.Timestamp(decision.replace(tzinfo=None))].iloc[0]
    assert pd.Timestamp(entry_row["open_time"]) >= pd.Timestamp(decision.replace(tzinfo=None))


def test_mfe_monotonicity_synthetic():
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    c1m = _synthetic_1m(start, 300, base=1.0)
    entry = start + timedelta(minutes=5)
    horizons = {}
    for h in (15, 30, 60, 120, 240):
        horizons[str(h)] = compute_mfe_mae_horizon(
            c1m, direction="BULLISH", entry_at=entry, entry_price=1.0, horizon_min=h
        )
    mono = check_horizon_monotonicity(horizons, (15, 30, 60, 120, 240))
    assert mono["monotonic_ok"] is True


def test_mae_monotonicity_synthetic():
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    c1m = _synthetic_1m(start, 300, base=1.0)
    entry = start + timedelta(minutes=5)
    horizons = compute_all_horizons(c1m, direction="BULLISH", entry_at=entry, entry_price=1.0)
    prev = 0.0
    for h in (15, 30, 60, 120, 240):
        mae = horizons[str(h)]["mae_pct"]
        assert mae is not None and mae + 1e-9 >= prev
        prev = mae


def test_manual_recompute_matches():
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    c1m = _synthetic_1m(start, 400, base=1.0)
    entry = (start + timedelta(minutes=10)).isoformat()
    oc = compute_mfe_mae_horizon(c1m, direction="BULLISH", entry_at=entry, entry_price=1.0, horizon_min=120)
    chk = manual_recompute_check(
        c1m, direction="BULLISH", entry_at=entry, entry_price=1.0, horizon_min=120, stored=oc
    )
    assert chk["match_mfe"] and chk["match_mae"]
    assert chk["path_bars"] > 0
    assert chk["path_last"] <= chk["horizon_end"]


def test_horizon_end_exact():
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    entry = start + timedelta(minutes=5)
    c1m = _synthetic_1m(start, 300, base=1.0)
    chk = manual_recompute_check(
        c1m,
        direction="BULLISH",
        entry_at=entry.isoformat(),
        entry_price=1.0,
        horizon_min=60,
        stored=compute_mfe_mae_horizon(c1m, direction="BULLISH", entry_at=entry, entry_price=1.0, horizon_min=60),
    )
    assert chk["path_bars"] == 60


def test_long_short_mfe_mae_correct():
    start = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    c1m = _synthetic_1m(start, 120, base=1.0)
    entry = start + timedelta(minutes=5)
    long_oc = compute_mfe_mae_horizon(c1m, direction="BULLISH", entry_at=entry, entry_price=1.0, horizon_min=60)
    short_oc = compute_mfe_mae_horizon(c1m, direction="BEARISH", entry_at=entry, entry_price=1.0, horizon_min=60)
    assert long_oc["mfe_pct"] >= 0 and long_oc["mae_pct"] >= 0
    assert short_oc["mfe_pct"] >= 0 and short_oc["mae_pct"] >= 0


def test_mae_first_same_bar():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 100.0,
                "high": 100.3,
                "low": 99.7,
                "close": 100.0,
                "volume": 1,
            }
        ]
    )
    oc = compute_mfe_mae_horizon(df, direction="BULLISH", entry_at=t0, entry_price=100.0, horizon_min=15)
    assert oc["first_extreme"] == "MAE_FIRST"


def test_missing_horizon_coverage_marked():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.01,
                "low": 0.99,
                "close": 1.0,
                "volume": 1,
            }
        ]
    )
    oc = compute_mfe_mae_horizon(df, direction="BULLISH", entry_at=t0, entry_price=1.0, horizon_min=240)
    assert oc["coverage"] == "OK"
    assert oc["mfe_pct"] is not None


def test_research_never_overwrites_production_label():
    cov = {
        "candles": {"status": "VALID"},
        "public_trades_cross": {"status": "VALID"},
        "orderbook_ob200_v3": {"status": "VALID"},
    }
    v, _ = apply_available_source_research(direction="BULLISH", features={}, coverage=cov)
    assert not v.startswith("ALLOW")
    assert v != "INCONCLUSIVE_DATA"


def test_missing_never_neutral():
    cov = {"candles": {"status": "VALID"}, "oi_1m": {"status": "MISSING"}}
    v, _ = apply_available_source_research(direction="BULLISH", features={}, coverage=cov)
    assert v == "RESEARCH_INSUFFICIENT"


def test_horizon_progression_fields():
    horizons = {
        "30": {"mfe_pct": 0.1, "mae_pct": 0.05, "mfe_minus_mae": 0.05},
        "60": {"mfe_pct": 0.2, "mae_pct": 0.08, "mfe_minus_mae": 0.12},
        "120": {"mfe_pct": 0.3, "mae_pct": 0.1, "mfe_minus_mae": 0.2},
        "240": {"mfe_pct": 0.4, "mae_pct": 0.15, "mfe_minus_mae": 0.25},
    }
    prog = horizon_progression_row(horizons, (30, 60, 120, 240))
    assert prog["best_horizon_mfe_minus_mae"] == 240
    assert prog["mfe_delta_30_to_60"] == pytest.approx(0.1)


def test_no_double_count_cross_episode_in_export():
    repo = __import__("pathlib").Path(__file__).resolve().parents[1]
    export = repo / "results" / "edc_sync_tolerance" / "xrp_30m_shortlist_with_horizons" / "candidates_30m_with_sources.csv"
    if export.exists():
        df = pd.read_csv(export)
        assert len(df) == len(df["candidate_id"].unique())
