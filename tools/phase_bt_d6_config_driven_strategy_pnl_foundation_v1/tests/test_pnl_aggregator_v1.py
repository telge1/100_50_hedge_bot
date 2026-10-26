from __future__ import annotations

import json
from pathlib import Path

import pytest

from config_validate_v1 import load_and_validate_config
from manifest_v1 import build_run_manifest, config_content_hash
from pnl_aggregator_v1 import SyntheticTrade, aggregate_pnl

BASELINE = Path(__file__).resolve().parents[1] / "configs/pool_landscape_baseline_v1.json"


def test_synthetic_pnl_metrics():
    cfg = load_and_validate_config(BASELINE)
    trades = [
        SyntheticTrade("A", "DOGEUSDT", "SHORT", 1.0, 0.9, "t0", "t1", "TP", 96),
        SyntheticTrade("B", "DOGEUSDT", "SHORT", 1.0, 1.1, "t2", "t3", "SL", 96),
    ]
    out = aggregate_pnl(cfg, trades)
    assert out["closed_trades"] == 2
    assert out["ending_equity_usdt"] != out["initial_equity_usdt"]
    assert out["trading_fees_usdt"] > 0
    assert len(out["equity_curve"]) >= 3
    assert out["max_drawdown_pct"] >= 0
    assert out["synthetic_only"] is True
    for key in (
        "gross_pnl_usdt",
        "net_pnl_usdt",
        "win_rate",
        "profit_factor",
    ):
        assert key in out


def test_slippage_and_funding_models():
    cfg = load_and_validate_config(BASELINE)
    cfg["pnl"]["slippage_model"] = {
        "model": "pct_of_notional_per_side",
        "entry_slippage_pct": 0.01,
        "exit_slippage_pct": 0.01,
    }
    cfg["pnl"]["funding_model"] = {"model": "constant_bps_per_8h_short", "funding_rate_bps_per_8h": 1.0}
    trades = [SyntheticTrade("C", "DOGEUSDT", "SHORT", 1.0, 0.95, "t0", "t1", "TP", 96)]
    out = aggregate_pnl(cfg, trades)
    assert out["slippage_cost_usdt"] > 0
    assert out["funding_fees_usdt"] > 0


def test_manifest_hash_deterministic():
    cfg = json.loads(BASELINE.read_text(encoding="utf-8"))
    h1 = config_content_hash(cfg)
    h2 = config_content_hash(cfg)
    assert h1 == h2
    m1 = build_run_manifest(cfg, run_kind="test")
    m2 = build_run_manifest(cfg, run_kind="test")
    assert m1["config_hash"] == m2["config_hash"]


def test_ambiguous_skipped():
    cfg = load_and_validate_config(BASELINE)
    trades = [
        SyntheticTrade(
            "D",
            "DOGEUSDT",
            "SHORT",
            1.0,
            1.0,
            "t0",
            "t1",
            "AMBIG",
            1,
            ambiguous_intrabar=True,
        )
    ]
    out = aggregate_pnl(cfg, trades)
    assert out["closed_trades"] == 0
