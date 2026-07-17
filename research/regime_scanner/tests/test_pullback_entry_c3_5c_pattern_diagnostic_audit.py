"""Tests for C3.5c APT pattern diagnostic audit (research-only)."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import numpy as np
import pandas as pd

from research.regime_scanner.pullback_entry_c3_5 import config_hash
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_pattern_diagnostic_audit import (
    DEFAULT_OUT,
    NUMERIC_FEATURE_COLS,
    _adx_bucket,
    _cross_age_bucket,
    _slope_bucket,
    assign_split,
    build_event_aligned_panel,
    cliffs_delta,
    enrich_diagnostic_frame,
    enrich_filled_from_entries,
    evaluate_diagnostic_candidates,
    extract_trade_features,
    fixed_chrono_splits,
    label_trades,
    pullback_context_summary,
    split_feature_direction,
    winner_loser_summary,
)
from research.regime_scanner.pullback_entry_c3_5c_realized_outcome_audit import (
    trades_exit_a_opposite_entry,
)
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import assign_split as rob_assign
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import fixed_chrono_splits as rob_splits


def test_output_path_no_strategy_promotion() -> None:
    assert "c35c_pattern_diagnostic_audit" in str(DEFAULT_OUT)
    src = Path("research/regime_scanner/pullback_entry_c3_5c_pattern_diagnostic_audit.py").read_text()
    assert "not an accepted strategy filter" in src.lower() or "no_filter_promotion" in src
    assert "no_filter_promotion" in src


def test_sm_and_pine_untouched_and_no_lookahead() -> None:
    sm_path = Path("research/regime_scanner/pullback_entry_c3_5.py")
    h1 = hashlib.sha256(sm_path.read_bytes()).hexdigest()
    import research.regime_scanner.pullback_entry_c3_5c_pattern_diagnostic_audit as mod

    _ = mod.DEFAULT_OUT
    h2 = hashlib.sha256(sm_path.read_bytes()).hexdigest()
    assert h1 == h2
    mod_src = Path(mod.__file__).read_text()
    assert "lookahead_on" not in mod_src
    assert "build_pullback_entry_pine" not in mod_src


def test_reuses_shared_exit_a_and_splits() -> None:
    src = inspect.getsource(
        __import__(
            "research.regime_scanner.pullback_entry_c3_5c_pattern_diagnostic_audit",
            fromlist=["*"],
        )
    )
    assert "trades_exit_a_opposite_entry" in src
    assert "generate_exit_a_trades" in src
    assert "build_extended_tf_frame" in src
    sp = fixed_chrono_splits(pd.Timestamp("2026-01-26", tz="UTC"), pd.Timestamp("2026-06-28", tz="UTC"))
    sp2 = rob_splits(pd.Timestamp("2026-01-26", tz="UTC"), pd.Timestamp("2026-06-28", tz="UTC"))
    assert sp["method"] == "60_20_20"
    assert sp["development_end"] == sp2["development_end"]
    t = pd.Timestamp("2026-03-01", tz="UTC")
    assert assign_split(t, sp) == rob_assign(t, sp2)


def _synthetic_ohlcv(n: int = 80) -> pd.DataFrame:
    """Minimal frame with indicator-like columns for unit tests (not full SM)."""
    ts = pd.date_range("2026-02-01", periods=n, freq="15min", tz="UTC")
    close = np.linspace(100, 110, n) + np.sin(np.linspace(0, 6, n))
    high = close + 0.5
    low = close - 0.5
    open_ = close - 0.1
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.ones(n),
            "symbol": ["T"] * n,
            "timeframe": ["15m"] * n,
            "bar_index": np.arange(n),
            "adx": np.linspace(15, 35, n),
            "plus_di": np.linspace(10, 30, n),
            "minus_di": np.linspace(25, 12, n),
            "atr_14": np.full(n, 1.0),
            "atr": np.full(n, 1.0),
            "ema_9": close - 0.2,
            "ema_20": close - 0.5,
            "ema_50": close - 1.0,
            "ema9_below_ema20": True,
            "ema9_above_ema20": False,
            "major_direction": np.where(np.arange(n) < n // 2, -1, 1),
            "protected_high": close + 2,
            "protected_low": close - 2,
            "active_external_break_level": close,
            "protected_structure_state": ["bearish_structure"] * n,
            "arm_edge_external_bear": [False] * n,
            "arm_edge_external_bull": [False] * n,
            "arm_edge_internal_bear": [False] * n,
            "arm_edge_internal_bull": [False] * n,
            "arm_edge_choch_bear": [False] * n,
            "arm_edge_choch_bull": [False] * n,
            "micro_swing_high": high,
            "micro_swing_low": low,
        }
    )
    df.loc[5, "arm_edge_external_bear"] = True
    df.loc[40, "arm_edge_external_bull"] = True
    return enrich_diagnostic_frame(df)


def test_enrich_frame_is_causal_no_future_leak() -> None:
    df = _synthetic_ohlcv(30)
    assert pd.isna(df["adx_change_3"].iloc[0])
    assert "atr_pct_rolling_rank" in df.columns


def test_label_trades_winners_splits_top3() -> None:
    splits = fixed_chrono_splits(pd.Timestamp("2026-02-01", tz="UTC"), pd.Timestamp("2026-05-01", tz="UTC"))
    rows = []
    base = pd.Timestamp("2026-02-01", tz="UTC")
    rets = [10.0, 5.0, 4.0, -1.0, -2.0, 0.5]  # top3 = 10,5,4
    for i, r in enumerate(rets):
        rows.append(
            {
                "symbol": "T",
                "timeframe": "15m",
                "side": "long" if i % 2 == 0 else "short",
                "setup_id": i + 1,
                "entry_timestamp": base + pd.Timedelta(days=i * 10),
                "exit_timestamp": base + pd.Timedelta(days=i * 10 + 1),
                "entry_price": 100.0,
                "exit_price": 100.0,
                "holding_bars": 4,
                "gross_return_pct": r + 0.2,
                "net_return_0_20_pct": r,
                "maximum_favorable_pct": abs(r),
                "maximum_adverse_pct": -1.0,
                "closed": True,
            }
        )
    rows.append(
        {
            "symbol": "T",
            "timeframe": "15m",
            "side": "long",
            "setup_id": 99,
            "entry_timestamp": base + pd.Timedelta(days=70),
            "exit_timestamp": base + pd.Timedelta(days=71),
            "entry_price": 100.0,
            "exit_price": 100.0,
            "holding_bars": 1,
            "gross_return_pct": 0.0,
            "net_return_0_20_pct": -0.2,
            "maximum_favorable_pct": 0.0,
            "maximum_adverse_pct": 0.0,
            "closed": False,
        }
    )
    lab = label_trades(pd.DataFrame(rows), splits)
    closed = lab[lab["closed"] == True]  # noqa: E712
    assert closed["top1_trade"].sum() == 1
    assert closed["top3_trade"].sum() == 3
    assert int(closed.loc[closed["top1_trade"], "net_return_0_20_pct"].iloc[0]) == 10
    assert set(lab["split"].unique()) <= {"development", "validation", "oos"}
    assert lab.loc[lab["closed"] == False, "winner_net020"].sum() == 0  # noqa: E712


def test_direction_normalization_long_short() -> None:
    assert _slope_bucket(0.2) == "with"
    assert _slope_bucket(-0.2) == "against"
    assert _slope_bucket(0.0) == "near_zero"
    assert _adx_bucket(18) == "<20"
    assert _adx_bucket(27) == "25-30"
    assert _cross_age_bucket(1, "bear") == "0-2"
    assert _cross_age_bucket(np.nan, "") == "no_valid_cross"


def test_event_panel_relative_zero_is_trigger_and_post_marked() -> None:
    frame = _synthetic_ohlcv(40)
    panel = pd.DataFrame(
        [
            {
                "trade_id": "t1",
                "side": "short",
                "split": "development",
                "winner_net020": True,
                "net_return_020_pct": 1.0,
                "closed": True,
                "feature_ok": True,
                "trigger_bar": 10,
            }
        ]
    )
    ep = build_event_aligned_panel(frame, panel, window=3)
    assert set(ep["relative_bar"]) == set(range(-3, 4))
    z = ep[ep["relative_bar"] == 0].iloc[0]
    assert bool(z["pre_entry"]) is True
    assert bool(z["post_entry"]) is False
    post = ep[ep["relative_bar"] > 0]
    assert post["post_entry"].all()
    assert (~post["pre_entry"]).all()
    assert "post_entry" not in NUMERIC_FEATURE_COLS


def test_exit_a_fill_next_open_semantics() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-02-01", periods=6, freq="15min", tz="UTC"),
            "open": [100.0, 101.0, 102.0, 99.0, 98.0, 97.0],
            "high": [103] * 6,
            "low": [96] * 6,
            "close": [100.5, 101.5, 101.0, 98.5, 97.5, 97.0],
            "symbol": ["T"] * 6,
        }
    )
    filled = [
        {
            "side": -1,
            "side_name": "short",
            "setup_id": 1,
            "trigger_bar": 0,
            "fill_bar": 1,
            "trigger_timestamp": frame.iloc[0]["timestamp"],
            "fill_timestamp": frame.iloc[1]["timestamp"],
            "entry_price": 101.0,
        },
        {
            "side": 1,
            "side_name": "long",
            "setup_id": 2,
            "trigger_bar": 3,
            "fill_bar": 4,
            "trigger_timestamp": frame.iloc[3]["timestamp"],
            "fill_timestamp": frame.iloc[4]["timestamp"],
            "entry_price": 98.0,
        },
    ]
    trades = trades_exit_a_opposite_entry(frame, filled, timeframe="15m", variant="A6")
    assert bool(trades.iloc[0]["closed"]) is True
    assert float(trades.iloc[0]["entry_price"]) == 101.0
    assert float(trades.iloc[0]["exit_price"]) == 98.0
    assert int(filled[0]["fill_bar"]) == int(filled[0]["trigger_bar"]) + 1


def test_dev_buckets_frozen_on_val_oos() -> None:
    splits = fixed_chrono_splits(pd.Timestamp("2026-02-01", tz="UTC"), pd.Timestamp("2026-05-01", tz="UTC"))
    base = pd.Timestamp("2026-02-01", tz="UTC")
    rows = []
    for i, (depth, ret, day) in enumerate(
        [
            (0.2, 1.0, 0),
            (0.3, -1.0, 5),
            (1.5, 2.0, 10),
            (1.6, -0.5, 15),
            (3.0, 0.5, 20),
            (3.1, -0.2, 25),
            (0.25, 0.1, 50),
            (2.9, -0.1, 70),
        ]
    ):
        rows.append(
            {
                "side": "long",
                "setup_id": i,
                "entry_timestamp": base + pd.Timedelta(days=day),
                "exit_timestamp": base + pd.Timedelta(days=day, hours=1),
                "holding_bars": 2,
                "gross_return_pct": ret + 0.2,
                "net_return_0_20_pct": ret,
                "closed": True,
                "timeframe": "15m",
                "pullback_depth_atr": depth,
                "pullback_duration_bars": 3,
                "chase_distance_atr": 0.5,
                "maximum_favorable_pct": 1.0,
                "maximum_adverse_pct": -1.0,
            }
        )
    panel = label_trades(pd.DataFrame(rows), splits)
    summary = pullback_context_summary(panel)
    assert not summary.empty
    for _, g in summary.groupby("feature"):
        assert g["dev_q33"].nunique() == 1
        assert g["dev_q66"].nunique() == 1


def test_winner_loser_and_candidates_status_values() -> None:
    splits = fixed_chrono_splits(pd.Timestamp("2026-02-01", tz="UTC"), pd.Timestamp("2026-05-01", tz="UTC"))
    base = pd.Timestamp("2026-02-01", tz="UTC")
    rng = np.random.default_rng(0)
    rows = []
    for i in range(24):
        win = i % 2 == 0
        row = {
            "side": "long" if i < 12 else "short",
            "setup_id": i,
            "entry_timestamp": base + pd.Timedelta(days=i * 3),
            "exit_timestamp": base + pd.Timedelta(days=i * 3 + 1),
            "holding_bars": 3,
            "gross_return_pct": (1.0 if win else -1.0) + 0.2,
            "net_return_0_20_pct": 1.0 if win else -1.0,
            "closed": True,
            "timeframe": "15m",
            "maximum_favorable_pct": 2.0,
            "maximum_adverse_pct": -1.0,
        }
        for c in NUMERIC_FEATURE_COLS:
            row[c] = float(rng.normal())
        row["adx"] = 30.0 if win else 15.0
        row["di_spread_signed"] = 5.0 if win else -2.0
        rows.append(row)
    panel = label_trades(pd.DataFrame(rows), splits)
    panel.loc[panel["winner_net020"], "adx"] = 30.0
    panel.loc[~panel["winner_net020"], "adx"] = 15.0
    wl = winner_loser_summary(panel, features=("adx", "di_spread_signed"))
    assert not wl.empty
    assert (
        wl.loc[wl["feature"] == "adx", "median_winner"].iloc[0]
        > wl.loc[wl["feature"] == "adx", "median_loser"].iloc[0]
    )
    sd = split_feature_direction(panel, features=("adx",))
    cand = evaluate_diagnostic_candidates(panel, wl, sd)
    assert set(cand["status"]).issubset(
        {"interesting_for_followup", "weak", "unstable", "top3_driven", "underpowered", "descriptive_only"}
    )
    assert "accepted" not in " ".join(cand["status"].astype(str)).lower()


def test_extract_features_uses_trigger_not_fill_and_missing_ok() -> None:
    frame = _synthetic_ohlcv(50)
    filled = [
        {
            "side": -1,
            "side_name": "short",
            "setup_id": 1,
            "trigger_bar": 10,
            "fill_bar": 11,
            "trigger_timestamp": frame.iloc[10]["timestamp"],
            "fill_timestamp": frame.iloc[11]["timestamp"],
            "entry_price": float(frame.iloc[11]["open"]),
            "pullback_high": float(frame.iloc[8]["high"]),
            "pullback_low": float(frame.iloc[9]["low"]),
            "armed_price": float(frame.iloc[5]["close"]),
        }
    ]
    lives = [
        {
            "setup_id": 1,
            "direction": "short",
            "arming_type": "external_bos",
            "armed_bar": 5,
            "armed_price": float(frame.iloc[5]["close"]),
            "pullback_bar": 8,
            "ready_bar": 9,
            "trigger_bar": 10,
            "fill_bar": 11,
            "entry_created": True,
        }
    ]
    trades = pd.DataFrame(
        [
            {
                "symbol": "T",
                "timeframe": "15m",
                "side": "short",
                "setup_id": 1,
                "entry_timestamp": frame.iloc[11]["timestamp"],
                "exit_timestamp": frame.iloc[20]["timestamp"],
                "entry_price": float(frame.iloc[11]["open"]),
                "exit_price": 100.0,
                "holding_bars": 9,
                "gross_return_pct": 1.0,
                "net_return_0_20_pct": 0.8,
                "closed": True,
                "split": "development",
                "month": "2026-02",
                "winner_net020": True,
                "loser_net020": False,
                "top1_trade": False,
                "top3_trade": False,
                "trade_id": "x",
                "maximum_favorable_pct": 2.0,
                "maximum_adverse_pct": -1.0,
            }
        ]
    )
    panel = extract_trade_features(frame, trades, filled, lives)
    assert bool(panel.iloc[0]["feature_ok"])
    assert int(panel.iloc[0]["trigger_bar"]) == 10
    assert int(panel.iloc[0]["fill_bar"]) == 11
    assert bool(panel.iloc[0]["entry_is_next_open"])
    assert bool(panel.iloc[0]["post_entry_used_as_entry_feature"]) is False
    assert abs(float(panel.iloc[0]["adx"]) - float(frame.iloc[10]["adx"])) < 1e-9


def test_cliffs_and_a6_hash_stable() -> None:
    d = cliffs_delta([3, 4, 5], [1, 2, 2])
    assert d is not None and d > 0
    cfg = baseline_a6()
    assert cfg.name == "A6"
    assert len(config_hash(cfg)) == 64


def test_enrich_filled_merges_entry_fields() -> None:
    filled = [{"setup_id": 1, "side_name": "long", "fill_bar": 2}]
    entries = [{"setup_id": 1, "pullback_high": 1.2, "pullback_low": 1.0, "armed_price": 1.1}]
    out = enrich_filled_from_entries(filled, entries)
    assert out[0]["pullback_high"] == 1.2


def test_deterministic_label_twice() -> None:
    splits = fixed_chrono_splits(pd.Timestamp("2026-02-01", tz="UTC"), pd.Timestamp("2026-05-01", tz="UTC"))
    df = pd.DataFrame(
        [
            {
                "symbol": "T",
                "timeframe": "15m",
                "side": "long",
                "setup_id": 1,
                "entry_timestamp": pd.Timestamp("2026-02-10", tz="UTC"),
                "exit_timestamp": pd.Timestamp("2026-02-11", tz="UTC"),
                "entry_price": 1.0,
                "exit_price": 1.1,
                "holding_bars": 2,
                "gross_return_pct": 10.2,
                "net_return_0_20_pct": 10.0,
                "maximum_favorable_pct": 11.0,
                "maximum_adverse_pct": -1.0,
                "closed": True,
            }
        ]
    )
    a = label_trades(df, splits)
    b = label_trades(df, splits)
    assert a["trade_id"].tolist() == b["trade_id"].tolist()
    assert a["split"].tolist() == b["split"].tolist()
