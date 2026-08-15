"""Population / wait-horizon tests for OI compression breakout (no DB)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.oi_compression_breakout.audit import build_candidate_tables
from research.regime_scanner.oi_compression_breakout.boxes import FrozenBox
from research.regime_scanner.oi_compression_breakout.breakouts import scan_breakout
from research.regime_scanner.oi_compression_breakout.config import MAX_WAIT_BARS, WAIT_WINDOWS, default_config
from research.regime_scanner.oi_compression_breakout.oi_groups import assign_oi_groups, compute_oi_features


def _frame(n: int = 80, *, break_at: int | None = None) -> pd.DataFrame:
    """Synthetic frame with confirm at 20, box 99.85–100.15, optional close break."""
    start = pd.Timestamp("2026-04-01T00:00:00Z")
    ts = [start + pd.Timedelta(minutes=5 * i) for i in range(n)]
    close = np.full(n, 100.0)
    high = np.full(n, 100.10)
    low = np.full(n, 99.90)
    # box window bars 4..19
    high[4:20] = 100.15
    low[4:20] = 99.85
    close[4:20] = 100.0
    if break_at is not None:
        j = 20 + break_at  # bars_to_breakout = break_at
        if j < n:
            close[j] = 100.20  # long break
            high[j] = 100.25
    open_ = np.full(n, 100.0)
    df = pd.DataFrame(
        {
            "symbol": "BTCUSDT",
            "bucket_start": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1.0,
            "open_interest": np.linspace(1000, 1100, n),
            "sequence_id": 1,
            "atr_14": np.full(n, 0.2),
            "atr_14_pctl_288": np.full(n, 50.0),
        }
    )
    return df


def _box(df: pd.DataFrame, confirm_i: int = 20, length: int = 16) -> FrozenBox:
    start_i = confirm_i - length
    return FrozenBox(
        symbol="BTCUSDT",
        sequence_id=1,
        box_length=length,
        quality="Q1",
        confirm_i=confirm_i,
        start_i=start_i,
        end_i=confirm_i - 1,
        box_high=100.15,
        box_low=99.85,
        box_width=0.30,
        box_width_atr=1.5,
        box_drift_ratio=0.0,
        inner_close_ratio=1.0,
        atr_14=0.2,
        atr_14_pctl_288=50.0,
        oi_start=1000.0,
        oi_end=1050.0,
        confirm_bucket=str(df["bucket_start"].iloc[confirm_i]),
        start_bucket=str(df["bucket_start"].iloc[start_i]),
        end_bucket=str(df["bucket_start"].iloc[confirm_i - 1]),
        physical_id="BTCUSDT|1|s|c",
        box_id="BTCUSDT|1|s|c|B16xQ1",
    )


@pytest.mark.parametrize("bars", [1, 20, 47])
def test_breakout_after_n_bars(bars: int):
    df = _frame(n=20 + bars + 5, break_at=bars)
    box = _box(df)
    br = scan_breakout(df, box, max_wait=MAX_WAIT_BARS)
    assert br["no_breakout"] is False
    assert br["bars_to_breakout"] == bars
    assert br["search_horizon_bars"] == 48
    assert br["fill_i"] == box.confirm_i + bars + 1
    for w in WAIT_WINDOWS:
        assert br[f"W{w}_any"] is (bars <= w)


def test_timeout_48_no_breakout_kept():
    df = _frame(n=20 + 60, break_at=None)
    box = _box(df)
    br = scan_breakout(df, box, max_wait=48)
    assert br["no_breakout"] is True
    assert br["breakout_side"] is None
    assert br["breakout_i"] is None
    assert br["fill_i"] is None
    assert br["fill_price"] is None
    assert br["outcome_status"] == "no_breakout_timeout"
    assert br["observed_search_bars"] == 48
    assert br["search_horizon_bars"] == 48
    for w in WAIT_WINDOWS:
        assert br[f"W{w}_any"] is False
        assert br[f"W{w}_none"] is True


def test_breakout_after_49_is_no_breakout_for_w48():
    df = _frame(n=20 + 55, break_at=49)
    box = _box(df)
    br = scan_breakout(df, box, max_wait=48)
    assert br["no_breakout"] is True
    assert br["W48_any"] is False
    assert br["outcome_status"] == "no_breakout_timeout"


def test_max_wait_must_cover_w48_in_detector():
    from research.regime_scanner.oi_compression_breakout.boxes import detect_frozen_boxes_with_early_release

    df = _frame(n=40, break_at=5)
    with pytest.raises(ValueError):
        detect_frozen_boxes_with_early_release(df, max_wait_bars=12)


def test_timeout_in_candidate_breakout_not_forward():
    df = _frame(n=80, break_at=None)
    box = _box(df)
    br = scan_breakout(df, box, max_wait=48)
    assert br["no_breakout"] is True
    feats = [compute_oi_features(df, box)]
    # force valid oi
    feats[0]["valid_oi"] = True
    feats[0]["oi_change_pct"] = 0.01
    feats[0]["positive_oi_step_ratio"] = 0.7
    oi_rows = assign_oi_groups([box], feats, min_history=1)
    assert any(r["oi_group"] == "O0" for r in oi_rows)
    cfg = default_config()
    cand_br, cand_fwd, _ = build_candidate_tables(
        boxes=[box],
        oi_rows=oi_rows,
        breakouts=[br],
        box_by_id={box.box_id: box},
        df=df,
        cfg=cfg,
    )
    assert len(cand_br) >= 1
    assert all(c["no_breakout"] for c in cand_br)
    assert cand_fwd == []


def test_gap_abort_status():
    df = _frame(n=40, break_at=None)
    # introduce gap after confirm
    df.loc[22, "bucket_start"] = df.loc[21, "bucket_start"] + pd.Timedelta(minutes=15)
    box = _box(df)
    br = scan_breakout(df, box, max_wait=48)
    assert br["no_breakout"] is True
    assert br["outcome_status"] == "gap_abort"
    assert br["invalidated"] is True


def test_sequence_end_status():
    df = _frame(n=40, break_at=None)
    df.loc[23, "sequence_id"] = 99
    box = _box(df)
    br = scan_breakout(df, box, max_wait=48)
    assert br["no_breakout"] is True
    assert br["outcome_status"] == "sequence_end"


def test_dataset_end_status():
    df = _frame(n=25, break_at=None)  # only 4 bars after confirm=20
    box = _box(df)
    br = scan_breakout(df, box, max_wait=48)
    assert br["no_breakout"] is True
    assert br["outcome_status"] == "dataset_end"
    assert br["observed_search_bars"] < 48


def test_confirm_before_scan_flag_on_detector():
    from research.regime_scanner.oi_compression_breakout.boxes import detect_frozen_boxes_with_early_release
    from research.regime_scanner.oi_compression_breakout.features import enrich_symbol_frame

    # use force_no_breakout style from main tests via local build
    n = 100
    start = pd.Timestamp("2026-04-01T00:00:00Z")
    ts = [start + pd.Timedelta(minutes=5 * i) for i in range(n)]
    close = np.full(n, 100.0)
    high = np.full(n, 100.15)
    low = np.full(n, 99.85)
    close[:] = 100.0
    # keep forever inside after a tight region — may or may not confirm; still check scan path
    df = pd.DataFrame(
        {
            "symbol": "BTCUSDT",
            "bucket_start": ts,
            "open": 100.0,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1.0,
            "open_interest": np.linspace(1000, 1200, n),
            "total_volume": 1.0,
            "sequence_id": 1,
            "data_available": True,
            "import_version": "derivatives_5m_v1",
        }
    )
    df = enrich_symbol_frame(df)
    boxes, breakouts, _ = detect_frozen_boxes_with_early_release(df, max_wait_bars=48)
    for br in breakouts:
        assert br.get("confirmed_before_breakout_scan") is True
        assert br.get("search_horizon_bars") == 48
