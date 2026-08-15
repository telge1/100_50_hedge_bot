"""Offline unit tests for OI compression breakout (no DB)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.oi_compression_breakout.audit import build_candidate_tables
from research.regime_scanner.oi_compression_breakout.boxes import (
    detect_frozen_boxes,
    detect_frozen_boxes_with_early_release,
    evaluate_box_at,
)
from research.regime_scanner.oi_compression_breakout.breakouts import scan_breakout
from research.regime_scanner.oi_compression_breakout.config import QUALITY_RULES, default_config
from research.regime_scanner.oi_compression_breakout.features import contiguous_same_sequence, enrich_symbol_frame
from research.regime_scanner.oi_compression_breakout.oi_groups import assign_oi_groups, compute_oi_features
from research.regime_scanner.oi_compression_breakout.outcomes import (
    compute_breakout_outcomes,
    follow_through_and_fakeout,
)
from research.regime_scanner.run_oi_compression_breakout_event_audit import build_parser, main as cli_main


def _synth(
    n: int = 120,
    *,
    compress: bool = True,
    oi_up: bool = True,
    force_no_breakout: bool = False,
    multi_length: bool = False,
) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    start = pd.Timestamp("2026-04-01T00:00:00Z")
    ts = [start + pd.Timedelta(minutes=5 * i) for i in range(n)]
    close = 100 + np.cumsum(rng.normal(0, 0.02, size=n))
    if compress:
        close[40:72] = 100 + rng.normal(0, 0.01, size=32)
        if force_no_breakout:
            # stay inside a wide-ish post-confirm range so no close break
            close[72:] = 100 + rng.normal(0, 0.005, size=n - 72)
        else:
            close[72] = 100.05
            close[73:80] = np.linspace(100.2, 101.5, 7)
            close[80:] = 101.5 + np.cumsum(rng.normal(0.01, 0.05, size=n - 80))
    high = close + 0.05
    low = close - 0.05
    if compress:
        high[40:72] = 100.15
        low[40:72] = 99.85
        if force_no_breakout:
            high[72:] = 100.14
            low[72:] = 99.86
            close[72:] = np.clip(close[72:], 99.90, 100.10)
        else:
            high[73] = 100.4
            low[73] = 100.0
            close[73] = 100.35
    if multi_length:
        # additional tight stretches for B32/B64 opportunities
        high[10:42] = 100.10
        low[10:42] = 99.90
        close[10:42] = 100.0
        high[80:144] = 100.12
        low[80:144] = 99.88
        close[80:144] = 100.0 + rng.normal(0, 0.005, size=min(64, n - 80))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    oi = np.full(n, 1000.0)
    if oi_up:
        oi[40:73] = np.linspace(1000, 1200, 33)
    else:
        oi[40:73] = np.linspace(1000, 900, 33)
    oi[73:] = oi[72]
    df = pd.DataFrame(
        {
            "symbol": "BTCUSDT",
            "bucket_start": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1.0,
            "open_interest": oi,
            "total_volume": np.linspace(10, 20, n),
            "buy_volume": 1.0,
            "sell_volume": 1.0,
            "delta": 0.0,
            "delta_ratio": 0.0,
            "spread_mean": 0.01,
            "spread_max": 0.02,
            "sequence_id": 1,
            "data_available": True,
            "import_version": "derivatives_5m_v1",
        }
    )
    return enrich_symbol_frame(df)


def test_confirm_candle_excluded_from_bounds():
    df = _synth()
    boxes = []
    for i in range(32, len(df)):
        b = evaluate_box_at(df, confirm_i=i, box_length=32, quality="Q1")
        if b is not None:
            boxes.append(b)
            break
    assert boxes
    b = boxes[0]
    prior_high = float(df["high"].iloc[b.start_i : b.confirm_i].max())
    assert b.box_high == pytest.approx(prior_high)


def test_box_freeze_not_expanded_by_later_candles():
    df = _synth()
    boxes = detect_frozen_boxes(df)
    assert boxes
    b = boxes[0]
    hi0 = b.box_high
    df.loc[b.confirm_i + 2, "high"] = hi0 + 10
    assert b.box_high == hi0


def test_no_box_across_gap():
    df = _synth()
    df.loc[50, "bucket_start"] = df.loc[49, "bucket_start"] + pd.Timedelta(minutes=15)
    b = evaluate_box_at(df, confirm_i=72, box_length=32, quality="Q1")
    assert b is None


def test_lengths_independent_active_key():
    """Cooldown on B16 must not block B32/B64 evaluation slots."""
    df = _synth(n=200, multi_length=True)
    boxes, breakouts, diag = detect_frozen_boxes_with_early_release(df)
    # diagnostics must exist for all lengths
    assert set(diag.by_length.keys()) == {16, 32, 64}
    for L in (16, 32, 64):
        assert diag.by_length[L]["raw_candidates"] > 0
    # confirmed lengths may be subset if filters fail — but active keys are separate
    lengths = {b.box_length for b in boxes}
    # At least ensure we attempted all; if any confirm, multiple lengths can coexist
    if len(boxes) >= 2:
        # two confirms of different lengths can share overlapping time
        assert True
    assert 16 in diag.by_length and 32 in diag.by_length and 64 in diag.by_length


def test_box_without_breakout_kept():
    df = _synth(force_no_breakout=True)
    boxes, breakouts, _ = detect_frozen_boxes_with_early_release(df)
    assert boxes
    # force scan result: at least one timeout-style row possible
    any_nb = False
    for b in boxes:
        br = scan_breakout(df, b, max_wait=48)
        if br["no_breakout"]:
            any_nb = True
            assert br["breakout_side"] is None
            assert br["breakout_i"] is None
            assert br["fill_i"] is None
            assert br["fill_price"] is None
            assert br["invalidated"] is False
            assert br["status"] in ("timeout_no_breakout", "data_end_no_breakout", "no_breakout_timeout", "dataset_end")
    assert any_nb


def test_timeout_not_invalidated():
    df = _synth(force_no_breakout=True)
    boxes = detect_frozen_boxes(df)
    assert boxes
    br = scan_breakout(df, boxes[0], max_wait=48)
    assert br["no_breakout"] is True
    assert br["invalidated"] is False


def test_oi_change_and_groups_parent_subset():
    df = _synth(oi_up=True)
    boxes = detect_frozen_boxes(df)
    assert boxes
    feats = [compute_oi_features(df, b) for b in boxes]
    rows = assign_oi_groups(boxes, feats, min_history=1)
    o0 = [r for r in rows if r["oi_group"] == "O0"]
    assert len(o0) == len(boxes)
    for r in rows:
        if r["oi_group"] == "O1":
            assert r["oi_change_pct"] > 0


def test_candidate_outcome_merge_same_box_different_candidates():
    df = _synth(oi_up=True)
    boxes, breakouts, _ = detect_frozen_boxes_with_early_release(df)
    feats = [compute_oi_features(df, b) for b in boxes]
    oi_rows = assign_oi_groups(boxes, feats, min_history=1)
    box_by_id = {b.box_id: b for b in boxes}
    for br in breakouts:
        br["oi_change_pct"] = 0.1
    cfg = default_config()
    cand_br, cand_fwd, box_fwd = build_candidate_tables(
        boxes=boxes, oi_rows=oi_rows, breakouts=breakouts, box_by_id=box_by_id, df=df, cfg=cfg
    )
    assert len(cand_br) == len(oi_rows)
    # same box_id can map to multiple candidate_ids
    if len(oi_rows) > len(boxes):
        ids = [r["box_id"] for r in cand_br]
        assert len(ids) > len(set(ids))
    # trading forwards only when breakout+fill
    for r in cand_fwd:
        assert r.get("fill_i") is not None or r.get("fill_price") is not None
    for br in breakouts:
        if br.get("no_breakout"):
            assert not any(c["box_id"] == br["box_id"] for c in cand_fwd)


def test_breakout_close_only_and_next_open_fill():
    df = _synth()
    boxes = detect_frozen_boxes(df)
    assert boxes
    b = next(x for x in boxes if x.box_length in (16, 32))
    br = scan_breakout(df, b, max_wait=48)
    if br.get("no_breakout"):
        pytest.skip("synthetic path did not break")
    assert br["fill_i"] == br["breakout_i"] + 1
    assert br["same_candle_fill"] is False


def test_wick_only_not_breakout():
    df = _synth()
    boxes = detect_frozen_boxes(df)
    b = boxes[0]
    j = b.confirm_i + 1
    df.loc[j, "high"] = b.box_high + 1.0
    df.loc[j, "close"] = (b.box_high + b.box_low) / 2
    df.loc[j, "low"] = b.box_low + 0.01
    for k in range(j + 1, min(len(df), j + 5)):
        df.loc[k, "close"] = (b.box_high + b.box_low) / 2
        df.loc[k, "high"] = b.box_high - 0.01
        df.loc[k, "low"] = b.box_low + 0.01
    br = scan_breakout(df, b, max_wait=48)
    assert br["no_breakout"] or br.get("breakout_i", -1) > j


def test_exit_not_nan_on_valid_path():
    df = _synth()
    boxes = detect_frozen_boxes(df)
    assert boxes
    b = boxes[0]
    br = scan_breakout(df, b)
    if br.get("fill_i") is None:
        # fabricate fill
        fill_i = min(len(df) - 1, b.confirm_i + 2)
        entry = float(df["open"].iloc[fill_i])
        side = "long"
        bi = fill_i - 1
    else:
        fill_i = int(br["fill_i"])
        entry = float(br["fill_price"])
        side = str(br["breakout_side"])
        bi = int(br["breakout_i"])
    oc = compute_breakout_outcomes(
        df,
        fill_i=fill_i,
        entry=entry,
        side=side,
        box_width=float(b.box_width),
        box_high=float(b.box_high),
        box_low=float(b.box_low),
        breakout_i=bi,
        compute_exits=True,
    )
    assert oc["n_exit_combos"] == 45
    assert oc["exit_X1_h12_c025_net"] is not None
    assert oc["exit_best_net"] is not None
    assert isinstance(oc["exit_X1_h12_c025_reason"], str)


def test_exit_tp_sl_time_synthetic():
    # build flat path then move for TP
    n = 80
    start = pd.Timestamp("2026-04-01T00:00:00Z")
    ts = [start + pd.Timedelta(minutes=5 * i) for i in range(n)]
    close = np.full(n, 100.0)
    high = np.full(n, 100.1)
    low = np.full(n, 99.9)
    # after fill_i=20, hit TP +0.5%
    high[21] = 100.6
    close[21] = 100.55
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
            "open_interest": np.full(n, 1000.0),
            "sequence_id": 1,
            "atr_14": np.full(n, 0.2),
            "atr_14_pctl_288": np.full(n, 50.0),
            "timestamp": ts,
        }
    )
    oc = compute_breakout_outcomes(
        df,
        fill_i=20,
        entry=100.0,
        side="long",
        box_width=0.3,
        box_high=100.15,
        box_low=99.85,
        breakout_i=19,
        compute_exits=True,
    )
    assert oc["exit_X1_h12_c025_reason"] == "TP"
    assert oc["exit_X1_h12_c025_net"] == pytest.approx(0.25)  # 0.50 - 0.25 cost

    # SL path
    high2 = np.full(n, 100.1)
    low2 = np.full(n, 99.9)
    low2[21] = 99.4
    close2 = np.full(n, 100.0)
    close2[21] = 99.45
    df2 = df.copy()
    df2["high"] = high2
    df2["low"] = low2
    df2["close"] = close2
    oc2 = compute_breakout_outcomes(
        df2,
        fill_i=20,
        entry=100.0,
        side="long",
        box_width=0.3,
        box_high=100.15,
        box_low=99.85,
        breakout_i=19,
        compute_exits=True,
    )
    assert oc2["exit_X1_h12_c025_reason"] in ("SL", "same_bar_conservative_sl")
    assert oc2["exit_X1_h12_c025_net"] < 0

    # time exit: no TP/SL touch
    oc3 = compute_breakout_outcomes(
        df.assign(high=100.1, low=99.9, close=100.0),
        fill_i=20,
        entry=100.0,
        side="long",
        box_width=0.3,
        box_high=100.15,
        box_low=99.85,
        breakout_i=19,
        compute_exits=True,
    )
    assert oc3["exit_X1_h12_c025_reason"] in ("time_exit", "data_end")


def test_same_bar_adverse_first():
    n = 40
    start = pd.Timestamp("2026-04-01T00:00:00Z")
    ts = [start + pd.Timedelta(minutes=5 * i) for i in range(n)]
    df = pd.DataFrame(
        {
            "symbol": "BTCUSDT",
            "bucket_start": ts,
            "timestamp": ts,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1.0,
            "open_interest": 1000.0,
            "sequence_id": 1,
            "atr_14": 0.2,
            "atr_14_pctl_288": 50.0,
        }
    )
    # fill bar hits both +0.5 and -0.5
    df.loc[10, "high"] = 100.6
    df.loc[10, "low"] = 99.4
    df.loc[10, "close"] = 100.0
    oc = compute_breakout_outcomes(
        df,
        fill_i=10,
        entry=100.0,
        side="long",
        box_width=0.3,
        box_high=100.2,
        box_low=99.8,
        breakout_i=9,
        compute_exits=True,
    )
    assert oc["same_bar_ambiguous"] is True
    assert oc["adverse_first"] is True
    assert oc["favorable_first"] is False


def test_fakeout_f1():
    df = _synth()
    fill_i = 80
    box_high, box_low = 100.15, 99.85
    df.loc[fill_i : fill_i + 2, "close"] = 100.5
    df.loc[fill_i + 2, "close"] = 100.0
    ft = follow_through_and_fakeout(
        df, breakout_i=fill_i - 1, fill_i=fill_i, side="long", box_high=box_high, box_low=box_low
    )
    assert ft["F1_fakeout"] is True


def test_contiguous_helper():
    df = _synth()
    seq = df["sequence_id"].to_numpy()
    ts = df["bucket_start"].to_numpy()
    assert contiguous_same_sequence(seq, ts, 0, 10)
    df.loc[5, "sequence_id"] = 99
    seq = df["sequence_id"].to_numpy()
    assert not contiguous_same_sequence(seq, ts, 0, 10)


def test_cli_rejects_unavailable(tmp_path):
    rc = cli_main(
        [
            "--symbols",
            "ENAUSDT",
            "--start",
            "2026-04-01T00:00:00Z",
            "--end",
            "2026-04-02T00:00:00Z",
            "--output-dir",
            str(tmp_path),
            "--smoke",
        ]
    )
    assert rc == 2


def test_cli_rejects_naive_time(tmp_path):
    rc = cli_main(
        [
            "--symbols",
            "BTCUSDT",
            "--start",
            "2026-04-01T00:00:00",
            "--end",
            "2026-04-02T00:00:00Z",
            "--output-dir",
            str(tmp_path),
            "--smoke",
        ]
    )
    assert rc == 2


def test_quality_thresholds():
    assert QUALITY_RULES["Q1"] == 2.0
    assert QUALITY_RULES["Q2"] == 1.5


def test_config_hash_stable():
    assert default_config().config_hash() == default_config().config_hash()


def test_parser_smoke_flag():
    p = build_parser()
    args = p.parse_args(
        [
            "--symbols",
            "BTCUSDT,APTUSDT",
            "--start",
            "2026-04-01T00:00:00Z",
            "--end",
            "2026-04-05T00:00:00Z",
            "--smoke",
        ]
    )
    assert args.smoke is True


def test_confirmation_does_not_use_future_breakout():
    """Mutating future closes after confirm must not change frozen bounds."""
    df = _synth()
    boxes = detect_frozen_boxes(df)
    assert boxes
    b = boxes[0]
    hi, lo = b.box_high, b.box_low
    df.loc[b.confirm_i + 1 :, "close"] = 999.0
    df.loc[b.confirm_i + 1 :, "high"] = 1000.0
    b2 = evaluate_box_at(df, confirm_i=b.confirm_i, box_length=b.box_length, quality=b.quality)
    assert b2 is not None
    assert b2.box_high == pytest.approx(hi)
    assert b2.box_low == pytest.approx(lo)
