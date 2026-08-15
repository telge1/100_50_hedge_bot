"""Offline unit tests for orderflow absorption audit (no DB)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.orderflow_absorption.audit import run_symbol
from research.regime_scanner.orderflow_absorption.compare import control_comparisons, summarize
from research.regime_scanner.orderflow_absorption.config import AbsorptionConfig, default_config
from research.regime_scanner.orderflow_absorption.features import compute_feature_rows, features_at, enrich_frame
from research.regime_scanner.orderflow_absorption.outcomes import forward_outcome_at
from research.regime_scanner.orderflow_absorption.patterns import (
    assignment_rows,
    close_location_class,
    flow_active,
    patterns_for_flow,
    price_reaction_for_negative_flow,
    price_reaction_for_positive_flow,
)
from research.regime_scanner.orderflow_absorption.reports import write_reports
from research.regime_scanner.run_orderflow_absorption_pattern_audit import build_parser


def _frame(
    n: int = 100,
    *,
    gap_at: int | None = None,
    price_ret_mode: str = "flat",
    delta_mode: str = "pos",
) -> pd.DataFrame:
    start = pd.Timestamp("2026-04-01T00:00:00Z")
    ts = [start + pd.Timedelta(minutes=5 * i) for i in range(n)]
    if gap_at is not None:
        for i in range(gap_at, n):
            ts[i] = ts[i] + pd.Timedelta(minutes=10)

    close = np.full(n, 100.0)
    if price_ret_mode == "up":
        close = 100 + np.linspace(0, 1.0, n)
    elif price_ret_mode == "down":
        close = 100 - np.linspace(0, 1.0, n)
    elif price_ret_mode == "flat":
        close = 100 + np.sin(np.linspace(0, 3, n)) * 0.02

    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + 0.05
    low = np.minimum(open_, close) - 0.05

    if delta_mode == "pos":
        buy, sell = np.full(n, 70.0), np.full(n, 30.0)
    elif delta_mode == "neg":
        buy, sell = np.full(n, 30.0), np.full(n, 70.0)
    else:
        buy, sell = np.full(n, 50.0), np.full(n, 50.0)
    tot = buy + sell
    delta = buy - sell
    oi = np.linspace(1000, 1050, n)

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
            "sequence_id": 1,
            "data_available": True,
            "import_version": "derivatives_5m_v1",
        }
    )


def test_parser():
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
    assert ns.lookbacks == "6,12,24"
    assert ns.move_thresholds == "0.0025,0.005,0.01"


def test_lookback_excludes_anchor():
    df = enrich_frame(_frame(delta_mode="pos", price_ret_mode="flat"), default_config())
    t = 40
    df.loc[t, "close"] = 200.0
    df.loc[t, "delta"] = 1e9
    df.loc[t, "buy_volume"] = 1e9
    df.loc[t, "total_volume"] = 1e9 + 1
    feat = features_at(df, t, 12)
    assert feat is not None
    assert feat["price_return"] < 0.01
    assert feat["delta_sum"] < 1e6


def test_gap_and_sequence():
    cfg = default_config()
    df = enrich_frame(_frame(gap_at=20), cfg)
    assert features_at(df, 25, 12) is None
    df2 = enrich_frame(_frame(), cfg)
    df2.loc[20:, "sequence_id"] = 2
    assert features_at(df2, 25, 12) is None


def test_delta_ratio():
    cfg = default_config()
    df = enrich_frame(_frame(delta_mode="pos"), cfg)
    feat = features_at(df, 30, 12)
    assert feat is not None
    assert feat["delta_ratio"] == pytest.approx(feat["delta_sum"] / feat["total_volume_sum"])
    assert feat["delta_ratio"] == pytest.approx(0.4)


def test_flow_f1_f2_f3():
    cfg = default_config()
    feat = {"abs_delta_ratio_p90_prior": 0.20}
    assert flow_active(0.12, "F1", feat, cfg) == (True, False)
    assert flow_active(-0.12, "F1", feat, cfg) == (False, True)
    assert flow_active(0.06, "F1", feat, cfg) == (False, False)
    assert flow_active(0.06, "F2", feat, cfg) == (True, False)
    assert flow_active(0.25, "F3", feat, cfg) == (True, False)
    assert flow_active(0.15, "F3", feat, cfg) == (False, False)


def test_price_reactions_and_close_loc():
    cfg = default_config()
    assert price_reaction_for_positive_flow(0.003, cfg) == "normal_progress"
    assert price_reaction_for_positive_flow(0.0005, cfg) == "weak_progress"
    assert price_reaction_for_positive_flow(-0.001, cfg) == "counter"
    assert price_reaction_for_negative_flow(-0.003, cfg) == "normal_progress"
    assert price_reaction_for_negative_flow(-0.0005, cfg) == "weak_progress"
    assert price_reaction_for_negative_flow(0.001, cfg) == "counter"
    assert close_location_class(0.30, flow_positive=True) == "weak_strong"
    assert close_location_class(0.70, flow_positive=False) == "strong_strong"


def test_patterns_a_and_c():
    cfg = default_config()
    # A1: pos flow + weak progress
    pats = [p[0] for p in patterns_for_flow(flow_rule="F1", pos_flow=True, neg_flow=False, price_return=0.0005, close_loc=0.8, cfg=cfg)]
    assert "A1" in pats and "C3" in pats and "C1" not in pats
    # C1: normal progress
    pats = [p[0] for p in patterns_for_flow(flow_rule="F1", pos_flow=True, neg_flow=False, price_return=0.003, close_loc=0.8, cfg=cfg)]
    assert "C1" in pats and "A1" not in pats
    # A2 via counter
    pats = [p[0] for p in patterns_for_flow(flow_rule="F1", pos_flow=True, neg_flow=False, price_return=-0.001, close_loc=0.8, cfg=cfg)]
    assert "A2" in pats
    # A3/A4 bullish
    pats = [p[0] for p in patterns_for_flow(flow_rule="F1", pos_flow=False, neg_flow=True, price_return=-0.0005, close_loc=0.2, cfg=cfg)]
    assert "A3" in pats and "C4" in pats
    pats = [p[0] for p in patterns_for_flow(flow_rule="F1", pos_flow=False, neg_flow=True, price_return=0.001, close_loc=0.2, cfg=cfg)]
    assert "A4" in pats


def test_outcome_t_plus_1_and_same_bar():
    df = _frame(n=50)
    t = 20
    df.loc[t, "high"] = 200.0
    df.loc[t, "close"] = 100.0
    df.loc[t + 1, "high"] = 100.6
    df.loc[t + 1, "low"] = 99.4
    df.loc[t + 1, "close"] = 100.0
    oc = forward_outcome_at(df, t, horizons=(3,), thresholds=(0.005,))
    assert oc is not None
    assert oc["h3_mfe_pct"] < 1.0
    assert oc["h3_0_50pct_same_bar"] is True
    assert oc["h3_0_50pct_bull_adv_first"] is True
    assert oc["h3_0_50pct_bear_adv_first"] is True
    assert oc["h3_0_50pct_bull_fav_first"] is False


def test_mfe_mae_side():
    df = _frame(n=40)
    t = 10
    df.loc[t, "close"] = 100.0
    df.loc[t + 1, "high"] = 101.0
    df.loc[t + 1, "low"] = 99.0
    df.loc[t + 1, "close"] = 100.2
    oc = forward_outcome_at(df, t, horizons=(1,), thresholds=(0.005,))
    assert oc["h1_mfe_pct"] == pytest.approx(1.0)
    assert oc["h1_mae_pct"] == pytest.approx(-1.0)
    assert oc["h1_bear_mfe"] == pytest.approx(1.0)
    assert oc["h1_bear_mae"] == pytest.approx(1.0)
    assert oc["h1_bull_edge"] == pytest.approx(0.0)


def test_no_duplicate_feature_keys():
    cfg = AbsorptionConfig(lookbacks=(6,), horizons=(3,), move_thresholds=(0.005,))
    feats = compute_feature_rows(_frame(n=80), cfg)
    keys = [(r["symbol"], r["timestamp"], r["lookback"]) for r in feats]
    assert len(keys) == len(set(keys))


def test_c5_always_and_deterministic(tmp_path: Path):
    cfg = AbsorptionConfig(lookbacks=(6,), horizons=(3,), move_thresholds=(0.005,), rolling_ref_bars=20)
    df = _frame(n=80, delta_mode="pos", price_ret_mode="flat")
    a = run_symbol(df, cfg)
    b = run_symbol(df, cfg)
    assert a["n_anchors"] == b["n_anchors"]
    assert [r["pattern"] for r in a["assignments"]] == [r["pattern"] for r in b["assignments"]]
    pats = {r["pattern"] for r in a["assignments"]}
    assert "C5" in pats


def test_comparisons_and_reports(tmp_path: Path):
    cfg = AbsorptionConfig(lookbacks=(6,), horizons=(3,), move_thresholds=(0.005,), rolling_ref_bars=20)
    # construct absorption-like: strong pos delta, flat price
    df = _frame(n=100, delta_mode="pos", price_ret_mode="flat")
    res = run_symbol(df, cfg)
    summary = summarize(res["assignments"], res["outcomes"], cfg)
    comps = control_comparisons(res["assignments"], res["outcomes"], cfg)
    assert not summary.empty
    payload = {
        "coverage": {"by_symbol": [{"symbol": "BTCUSDT", "joined_rows": len(df)}]},
        "joined_rows": len(df),
        "features": res["features"],
        "assignments": res["assignments"],
        "outcomes": res["outcomes"],
        "summary": summary.to_dict("records"),
        "comparisons": comps.to_dict("records") if not comps.empty else [],
        "coin_summary": [],
        "lookback_summary": [],
        "oi_diagnostic": [],
        "pattern_counts": {"C5": 1},
        "pattern_counts_f1": {"C5": 1},
        "n_feature_rows": res["n_anchors"],
        "by_symbol": {"BTCUSDT": {"joined_rows": len(df), "anchors": res["n_anchors"]}},
        "decision": "NO_USEFUL_ABSORPTION_PATTERN",
        "decision_rationale": "test",
        "config_hash": cfg.config_hash(),
        "cfg": cfg.to_dict(),
        "db_writes": False,
    }
    files = write_reports(tmp_path, payload)
    assert "integrity.json" in files
    assert '"db_writes": false' in (tmp_path / "integrity.json").read_text()
