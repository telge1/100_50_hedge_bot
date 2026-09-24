"""Unit tests for dual TPO + Volume dashboard adapter."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

DASH = Path(__file__).resolve().parent
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

from market_profile_v1.dual_profile import (  # noqa: E402
    DUAL_CONTRACT_VERSION,
    _tpo_bins_for_ui,
    _volume_bins_for_ui,
    build_dual_window_profile,
)


def test_tpo_bins_use_tpo_count_only():
    tpo = {
        "provenance": {"price_increment": 10.0},
        "rows": [
            {"price_bin_index": 7854, "price": 78545.0, "tpo_count": 7},
            {"price_bin_index": 7856, "price": 78565.0, "tpo_count": 3},
        ],
    }
    bins = _tpo_bins_for_ui(tpo)
    assert len(bins) == 2
    assert bins[0]["tpo_count"] == 7
    assert "base_volume" not in bins[0]


def test_volume_bins_use_base_volume_and_sides():
    vol = {
        "rows": [
            {
                "price_bin_index": 7856,
                "price_bin_low": 78560.0,
                "price_bin_high": 78570.0,
                "display_price": 78565.0,
                "base_volume": 12.5,
                "taker_buy_base_volume": 7.0,
                "taker_sell_base_volume": 5.5,
                "delta_base_volume": 1.5,
                "trade_count": 42,
            }
        ]
    }
    bins = _volume_bins_for_ui(vol)
    assert bins[0]["base_volume"] == 12.5
    assert bins[0]["buy_volume"] == 7.0
    assert bins[0]["sell_volume"] == 5.5


def test_build_dual_window_profile_monkeypatched(monkeypatch):
    window = MagicMock()
    window.start = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)
    window.end = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    window.window_id = "us_developing_to_anchor"
    window.to_dict = lambda: {"label": "US→anchor", "window_id": "us_developing_to_anchor"}

    trades = [
        {
            "ts": datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc),
            "trade_id": "1",
            "side": "Buy",
            "price": 79000.0,
            "size": 1.0,
            "notional": 79000.0,
        }
    ]

    monkeypatch.setattr(
        "market_profile_v1.dual_profile.load_window_trades",
        lambda *a, **k: trades,
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.tpo_profile.build_tpo_profile_from_trades",
        lambda *a, **k: {
            "tpo_profile_status": "COMPUTED_SEPARATELY",
            "provenance": {"price_increment": 10.0, "profile_kind": "TPO_BRACKET"},
            "tpoc": {"tpoc_price": 78545.0},
            "value_area": {"tpoc_vah": 79080.0, "tpoc_val": 78230.0, "actual_value_area_share": 0.706},
            "rows": [{"price_bin_index": 7854, "price": 78545.0, "tpo_count": 5}],
            "brackets": {"total_count": 11},
            "hvn_candidates": [],
            "lvn_candidates": [],
        },
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.volume_profile.build_volume_profile_from_trades",
        lambda *a, **k: {
            "volume_profile_status": "COMPUTED_SEPARATELY",
            "provenance": {"price_increment": 10.0, "primary_volume_basis": "base_volume"},
            "vpoc": {"vpoc_price": 78565.0},
            "value_area": {"vvah": 79140.0, "vval": 78190.0, "actual_value_area_share": 0.701},
            "rows": [
                {
                    "price_bin_index": 7856,
                    "price_bin_low": 78560.0,
                    "price_bin_high": 78570.0,
                    "display_price": 78565.0,
                    "base_volume": 9.0,
                    "taker_buy_base_volume": 5.0,
                    "taker_sell_base_volume": 4.0,
                    "delta_base_volume": 1.0,
                    "trade_count": 3,
                }
            ],
            "hvn_candidates": [],
            "lvn_candidates": [],
        },
    )
    monkeypatch.setattr(
        "market_profile_v1.dual_profile._classify_shape_from_volume",
        lambda *a, **k: type("S", (), {"to_dict": lambda self: {"kind": "BALANCE", "letter": "B"}})(),
    )
    out = build_dual_window_profile(
        MagicMock(),
        "BTCUSDT",
        window,
        value_area_pct=0.70,
        target_bins=160,
        use_final=False,
        thresholds=MagicMock(
            hvn_factor=1.5,
            lvn_factor=0.5,
            node_min_separation_bins=3,
            single_print_frac=0.15,
        ),
        include_bins=True,
        trades=trades,
    )
    assert out is not None
    assert out["dual_contract_version"] == DUAL_CONTRACT_VERSION
    assert out["tpo"]["value_area"]["poc"] == 78545.0
    assert out["volume"]["value_area"]["poc"] == 78565.0
    assert out["tpo"]["bins"][0]["tpo_count"] == 5
    assert out["volume"]["bins"][0]["base_volume"] == 9.0
    assert "bins" not in out
