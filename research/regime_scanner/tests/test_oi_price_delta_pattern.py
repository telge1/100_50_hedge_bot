"""Offline unit tests for OI + price + delta pattern audit (no DB)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.oi_price_delta_pattern.audit import run_symbol
from research.regime_scanner.oi_price_delta_pattern.compare import pattern_comparisons, summarize_patterns
from research.regime_scanner.oi_price_delta_pattern.config import PatternConfig, default_config
from research.regime_scanner.oi_price_delta_pattern.features import compute_feature_rows, features_at
from research.regime_scanner.oi_price_delta_pattern.outcomes import forward_outcome_at
from research.regime_scanner.oi_price_delta_pattern.reports import write_reports
from research.regime_scanner.oi_price_delta_pattern.states import (
    assign_states,
    delta_state,
    oi_state,
    patterns_for_row,
    price_state,
)
from research.regime_scanner.run_oi_price_delta_pattern_audit import build_parser


def _frame(
    n: int = 80,
    *,
    seq: int = 1,
    gap_at: int | None = None,
    price_mode: str = "flat",
    oi_mode: str = "up",
    delta_mode: str = "pos",
) -> pd.DataFrame:
    start = pd.Timestamp("2026-04-01T00:00:00Z")
    ts = [start + pd.Timedelta(minutes=5 * i) for i in range(n)]
    if gap_at is not None:
        for i in range(gap_at, n):
            ts[i] = ts[i] + pd.Timedelta(minutes=10)

    close = np.full(n, 100.0)
    if price_mode == "up":
        close = 100 + np.linspace(0, 2.0, n)  # ~2% over full series
    elif price_mode == "down":
        close = 100 - np.linspace(0, 2.0, n)
    elif price_mode == "flat":
        close = 100 + np.sin(np.linspace(0, 4, n)) * 0.05

    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = close + 0.1
    low = close - 0.1

    oi = np.full(n, 1000.0)
    if oi_mode == "up":
        oi = np.linspace(1000, 1100, n)
    elif oi_mode == "down":
        oi = np.linspace(1100, 1000, n)

    if delta_mode == "pos":
        buy, sell = np.full(n, 60.0), np.full(n, 40.0)
    elif delta_mode == "neg":
        buy, sell = np.full(n, 40.0), np.full(n, 60.0)
    else:
        buy, sell = np.full(n, 50.0), np.full(n, 50.0)
    tot = buy + sell
    delta = buy - sell

    return pd.DataFrame(
        {
            "symbol": "BTCUSDT",
            "bucket_start": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": tot,
            "open_interest": oi,
            "buy_volume": buy,
            "sell_volume": sell,
            "total_volume": tot,
            "delta": delta,
            "delta_ratio": delta / tot,
            "sequence_id": seq,
            "data_available": True,
            "import_version": "derivatives_5m_v1",
        }
    )


def test_parser_defaults():
    p = build_parser()
    ns = p.parse_args(
        [
            "--symbols",
            "BTCUSDT",
            "--start",
            "2026-03-15T00:00:00Z",
            "--end",
            "2026-05-06T00:00:00Z",
            "--output-dir",
            "/tmp/x",
        ]
    )
    assert ns.lookbacks == "12,24"
    assert ns.move_thresholds == "0.005,0.01"


def test_lookback_excludes_anchor_bar():
    df = _frame(price_mode="flat", oi_mode="up", delta_mode="pos")
    # Make bar t extreme so including it would change return/oi/delta
    t = 30
    df.loc[t, "close"] = 200.0
    df.loc[t, "open"] = 200.0
    df.loc[t, "open_interest"] = 9999.0
    df.loc[t, "delta"] = 9999.0
    df.loc[t, "buy_volume"] = 9999.0
    df.loc[t, "total_volume"] = 10000.0
    feat = features_at(df.assign(atr_14=1.0), t, lookback=12)
    assert feat is not None
    assert feat["price_return"] < 0.01  # not contaminated by 200 close
    assert feat["oi_end"] < 2000
    assert feat["delta_sum"] < 500


def test_gap_blocks_features():
    df = _frame(gap_at=20)
    df = df.assign(atr_14=1.0)
    assert features_at(df, 25, 12) is None


def test_sequence_change_blocks_features():
    df = _frame()
    df.loc[20:, "sequence_id"] = 2
    df = df.assign(atr_14=1.0)
    assert features_at(df, 25, 12) is None


def test_oi_change_pct():
    df = _frame(oi_mode="up").assign(atr_14=1.0)
    feat = features_at(df, 24, 12)
    assert feat is not None
    expected = feat["oi_end"] / feat["oi_start"] - 1.0
    assert feat["oi_change_pct"] == pytest.approx(expected)


def test_delta_and_ratio():
    df = _frame(delta_mode="pos").assign(atr_14=1.0)
    feat = features_at(df, 24, 12)
    assert feat is not None
    assert feat["delta_sum"] == pytest.approx(12 * 20.0)
    assert feat["delta_ratio"] == pytest.approx(feat["delta_sum"] / feat["total_volume_sum"])


def test_states():
    cfg = default_config()
    assert price_state(0.003, flat_abs=cfg.price_flat_abs) == "price_up"
    assert price_state(-0.003, flat_abs=cfg.price_flat_abs) == "price_down"
    assert price_state(0.001, flat_abs=cfg.price_flat_abs) == "price_flat"
    assert oi_state(0.01, oi_valid=True) == "oi_up"
    assert oi_state(-0.01, oi_valid=True) == "oi_down"
    assert oi_state(0.0, oi_valid=True) == "oi_flat"
    assert oi_state(0.01, oi_valid=False) == "oi_invalid"
    assert delta_state(0.06, neutral_abs=cfg.delta_neutral_abs) == "delta_positive"
    assert delta_state(-0.06, neutral_abs=cfg.delta_neutral_abs) == "delta_negative"
    assert delta_state(0.01, neutral_abs=cfg.delta_neutral_abs) == "delta_neutral"


def test_patterns_p1_to_p6():
    base = {
        "price_state": "price_flat",
        "oi_state": "oi_up",
        "delta_state": "delta_positive",
    }
    assert "P1" in patterns_for_row(base) and "P6" in patterns_for_row(base)
    assert "P2" in patterns_for_row({**base, "delta_state": "delta_negative"})
    assert "P3" in patterns_for_row(
        {"price_state": "price_up", "oi_state": "oi_up", "delta_state": "delta_positive"}
    )
    assert "P4" in patterns_for_row(
        {"price_state": "price_down", "oi_state": "oi_up", "delta_state": "delta_negative"}
    )
    assert "P5" in patterns_for_row(
        {"price_state": "price_flat", "oi_state": "oi_down", "delta_state": "delta_neutral"}
    )


def test_outcome_starts_at_t_plus_1():
    df = _frame(n=40)
    # spike only on bar t itself — must not count as future MFE
    t = 20
    df.loc[t, "high"] = 200.0
    df.loc[t, "close"] = 100.0
    # future mild up
    df.loc[t + 1 : t + 3, "high"] = 100.6
    df.loc[t + 1 : t + 3, "close"] = 100.5
    oc = forward_outcome_at(df, t, horizons=(3,), thresholds=(0.005,))
    assert oc is not None
    assert oc["h3_mfe_pct"] < 1.0  # not 100% from bar t


def test_up_down_first_and_same_bar():
    df = _frame(n=40)
    t = 10
    # bar t+1 hits both +0.5% and -0.5%
    df.loc[t + 1, "high"] = 100.6
    df.loc[t + 1, "low"] = 99.4
    df.loc[t + 1, "close"] = 100.0
    oc = forward_outcome_at(df, t, horizons=(3,), thresholds=(0.005,))
    assert oc is not None
    assert oc["h3_0_50pct_same_bar"] is True
    assert oc["h3_0_50pct_down_first"] is True
    assert oc["h3_0_50pct_up_first"] is False


def test_mfe_mae():
    df = _frame(n=40)
    t = 10
    df.loc[t, "close"] = 100.0
    df.loc[t + 1, "high"] = 101.0
    df.loc[t + 1, "low"] = 99.0
    df.loc[t + 1, "close"] = 100.5
    df.loc[t + 2, "high"] = 100.8
    df.loc[t + 2, "low"] = 99.5
    df.loc[t + 2, "close"] = 100.2
    oc = forward_outcome_at(df, t, horizons=(2,), thresholds=(0.005,))
    assert oc["h2_mfe_pct"] == pytest.approx(1.0)
    assert oc["h2_mae_pct"] == pytest.approx(-1.0)
    assert oc["h2_edge"] == pytest.approx(0.0)


def test_no_duplicate_feature_keys():
    df = _frame(n=60)
    cfg = PatternConfig(lookbacks=(12,), horizons=(3,), move_thresholds=(0.005,))
    feats = compute_feature_rows(df, cfg.lookbacks)
    keys = [(r["symbol"], r["timestamp"], r["lookback"]) for r in feats]
    assert len(keys) == len(set(keys))


def test_run_symbol_deterministic(tmp_path: Path):
    df = _frame(n=60, price_mode="flat", oi_mode="up", delta_mode="pos")
    cfg = default_config()
    a = run_symbol(df, cfg)
    b = run_symbol(df, cfg)
    assert a["n_anchors"] == b["n_anchors"]
    assert len(a["features"]) == len(b["features"])
    assert [r["pattern"] for r in a["assignments"]] == [r["pattern"] for r in b["assignments"]]
    # NaN-safe: compare finite price_return series
    ar = [r["price_return"] for r in a["features"]]
    br = [r["price_return"] for r in b["features"]]
    assert ar == br
    assert [r.get("h3_mfe_pct") for r in a["outcomes"]] == [r.get("h3_mfe_pct") for r in b["outcomes"]]


def test_comparisons_p1_vs_p6():
    df = _frame(n=80, price_mode="flat", oi_mode="up", delta_mode="pos")
    cfg = PatternConfig(lookbacks=(12,), horizons=(3,), move_thresholds=(0.005,))
    res = run_symbol(df, cfg)
    summary = summarize_patterns(res["assignments"], res["outcomes"], cfg)
    comps = pattern_comparisons(res["assignments"], res["outcomes"], res["features"], cfg)
    assert not summary.empty
    assert any(c.startswith("P1_vs_P6") or c == "P1_vs_P6" for c in comps["comparison"].unique())


def test_write_reports_no_db(tmp_path: Path):
    df = _frame(n=60)
    cfg = PatternConfig(lookbacks=(12,), horizons=(3,), move_thresholds=(0.005,))
    res = run_symbol(df, cfg)
    payload = {
        "coverage": {"by_symbol": [{"symbol": "BTCUSDT", "joined_rows": len(df)}]},
        "joined_rows": len(df),
        "features": res["features"],
        "assignments": res["assignments"],
        "outcomes": res["outcomes"],
        "summary": summarize_patterns(res["assignments"], res["outcomes"], cfg).to_dict("records"),
        "comparisons": [],
        "coin_summary": [],
        "direction_summary": [],
        "pattern_counts": {"P6": 10},
        "n_feature_rows": res["n_anchors"],
        "by_symbol": {"BTCUSDT": {"joined_rows": len(df), "anchors": res["n_anchors"]}},
        "decision": "NO_USEFUL_OI_PRICE_DELTA_PATTERN",
        "decision_rationale": "test",
        "config_hash": cfg.config_hash(),
        "cfg": cfg.to_dict(),
        "db_writes": False,
    }
    files = write_reports(tmp_path, payload)
    assert "integrity.json" in files
    integrity = (tmp_path / "integrity.json").read_text()
    assert '"db_writes": false' in integrity or '"db_writes": false' in integrity.lower()
    assert "REPORT.md" in files


def test_assign_states_roundtrip():
    cfg = default_config()
    feat = {
        "price_return": 0.0,
        "oi_change_pct": 0.01,
        "oi_valid": True,
        "delta_ratio": 0.1,
    }
    row = assign_states(feat, cfg)
    assert row["price_state"] == "price_flat"
    assert row["oi_state"] == "oi_up"
    assert row["delta_state"] == "delta_positive"
    assert "P1" in patterns_for_row(row)
