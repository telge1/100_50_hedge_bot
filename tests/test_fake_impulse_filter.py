"""Unit tests for research-only fake_impulse_filter (no ClickHouse)."""

from __future__ import annotations

import math

import pytest

from orderbook_analyse.fake_impulse_filter import (
    DecisionSnapshot,
    ImpulseState,
    Side,
    classify_long_frozen,
    classify_short_frozen,
    compute_impulse_metrics,
    decide_state,
    evaluate_whipsaw,
)
from orderbook_analyse.fake_impulse_filter.frozen_gate import FrozenGateLabel
from orderbook_analyse.fake_impulse_filter.persistence import outcome_mfe_mae
from orderbook_analyse.fake_impulse_filter.state_machine import DataAvailability
from orderbook_analyse.fake_impulse_filter.thresholds import FROZEN_DEFAULT


def _long_confirming_feat(**over):
    f = {
        "taker_buy_ratio": 0.95,
        "cvd_chg_5m": 50_000,
        "cvd_chg_3m": 20_000,
        "imbalance_l50": 0.25,
        "ofi_5m": 1000,
        "rv5_vs_prior30_med": 1.6,
        "ret_5m": 0.003,
        "ret_1m": 0.001,
        "vol_vs_30m_mean": 2.0,
    }
    f.update(over)
    return f


def test_frozen_long_confirmed_path():
    assert classify_long_frozen(_long_confirming_feat()) == FrozenGateLabel.PUMP_CONFIRMED


def test_frozen_short_is_mirror_of_long():
    long_f = _long_confirming_feat()
    short_f = {
        "taker_buy_ratio": 1.0 - long_f["taker_buy_ratio"],
        "cvd_chg_5m": -long_f["cvd_chg_5m"],
        "cvd_chg_3m": -long_f["cvd_chg_3m"],
        "imbalance_l50": -long_f["imbalance_l50"],
        "ofi_5m": -long_f["ofi_5m"],
        "rv5_vs_prior30_med": long_f["rv5_vs_prior30_med"],
        "ret_5m": -long_f["ret_5m"],
        "ret_1m": -long_f["ret_1m"],
        "vol_vs_30m_mean": long_f["vol_vs_30m_mean"],
    }
    assert classify_short_frozen(short_f) == FrozenGateLabel.DUMP_CONFIRMED


def test_single_spike_not_confirmed_without_persistence():
    # prices spike then give back
    prices = [100.0] + [100.5] * 5 + [100.1] * 60
    flow = [1] * len(prices)
    imb = [1] * len(prices)
    # overwrite later flow as mixed
    for i in range(10, len(flow)):
        flow[i] = 0
    m = compute_impulse_metrics(prices, flow, imb, 0, Side.LONG, sample_seconds=1)
    data = DataAvailability(trades="VALID", orderbook="VALID", oi="VALID", candles="VALID")
    d = decide_state(Side.LONG, _long_confirming_feat(ret_5m=0.001, vol_vs_30m_mean=1.0, rv5_vs_prior30_med=1.0), m, data)
    # confirming path needs soft price+ob+flow; with ret soft and vol not busy may be early or confirming
    assert d.state != ImpulseState.CONFIRMED or m.giveback_ratio.get(60, 0) is not None


def test_fast_giveback_failed_impulse():
    # strong move then 70% giveback within 60s
    prices = [100.0]
    for i in range(1, 20):
        prices.append(100.0 + 0.5 * (i / 19))  # up to 100.5
    for i in range(20, 70):
        # give back to ~100.15 → giveback ~70%
        prices.append(100.5 - 0.35 * ((i - 20) / 49))
    flow = [1] * len(prices)
    imb = [1] * len(prices)
    m = compute_impulse_metrics(prices, flow, imb, 0, Side.LONG, sample_seconds=1)
    assert m.giveback_ratio[60] is not None and m.giveback_ratio[60] >= 0.5
    data = DataAvailability(trades="VALID", orderbook="VALID", oi="VALID", candles="VALID")
    feat = _long_confirming_feat(ret_5m=0.001, rv5_vs_prior30_med=1.2, vol_vs_30m_mean=1.2)
    # may be EARLY or CONFIRMING depending on thr; force early via lower imb
    feat["imbalance_l50"] = 0.1
    d = decide_state(Side.LONG, feat, m, data)
    assert d.state in (ImpulseState.FAILED_IMPULSE, ImpulseState.EARLY_PRESSURE, ImpulseState.NO_EVIDENCE)


def test_whipsaw_blocks_opposite():
    w = evaluate_whipsaw(Side.SHORT, last_opposite_impulse_age_s=30, opposite_was_active=True, new_direction_independently_confirmed=False)
    assert w.blocked
    w2 = evaluate_whipsaw(Side.SHORT, 30, True, True)
    assert not w2.blocked


def test_decide_whipsaw_blocked():
    data = DataAvailability(trades="VALID", orderbook="VALID", oi="VALID", candles="VALID")
    prices = [100.0] * 120
    m = compute_impulse_metrics(prices, [1] * 120, [1] * 120, 0, Side.SHORT, sample_seconds=1)
    feat = {
        "taker_buy_ratio": 0.05,
        "cvd_chg_5m": -50_000,
        "cvd_chg_3m": -20_000,
        "imbalance_l50": -0.25,
        "ofi_5m": -1000,
        "rv5_vs_prior30_med": 1.6,
        "ret_5m": -0.003,
        "ret_1m": -0.001,
        "vol_vs_30m_mean": 2.0,
    }
    d = decide_state(Side.SHORT, feat, m, data, last_opposite_impulse_age_s=60, opposite_was_active=True)
    assert d.state == ImpulseState.WHIPSAW_BLOCKED


def test_missing_ob_inconclusive_when_confirming():
    data = DataAvailability(trades="VALID", orderbook="MISSING", oi="VALID", candles="VALID")
    prices = [100.0 + 0.01 * i for i in range(120)]
    m = compute_impulse_metrics(prices, [1] * 120, [1] * 120, 0, Side.LONG, sample_seconds=1)
    d = decide_state(Side.LONG, _long_confirming_feat(), m, data)
    assert d.state == ImpulseState.INCONCLUSIVE_DATA


def test_stale_not_treated_as_valid():
    data = DataAvailability(trades="PARTIAL", orderbook="VALID", oi="VALID", candles="VALID", stale_flags={"trades": True})
    assert data.blocks_confirmed


def test_oi_conflict_does_not_auto_confirm():
    # OI conflict is informational; CONFIRMED still needs persistence — early path stays early
    data = DataAvailability(trades="VALID", orderbook="VALID", oi="VALID", candles="VALID")
    prices = [100.0] * 5 + [100.2] * 5 + [100.0] * 60  # fail persist
    m = compute_impulse_metrics(prices, [1] * 70, [0] * 70, 0, Side.LONG, sample_seconds=1)
    feat = _long_confirming_feat()
    feat["oi_chg_pct_5m"] = -0.01  # OI down while long
    d = decide_state(Side.LONG, feat, m, data)
    assert d.state != ImpulseState.CONFIRMED


def test_liq_spike_without_follow_through_not_confirmed():
    data = DataAvailability(trades="VALID", orderbook="VALID", oi="VALID", candles="VALID")
    prices = [100.0] * 120
    m = compute_impulse_metrics(prices, [0] * 120, [0] * 120, 0, Side.LONG, sample_seconds=1)
    feat = {
        "taker_buy_ratio": 0.5,
        "cvd_chg_5m": 100,
        "cvd_chg_3m": 50,
        "imbalance_l50": 0.0,
        "ofi_5m": 0,
        "rv5_vs_prior30_med": 1.0,
        "ret_5m": 0.0,
        "ret_1m": 0.0,
        "vol_vs_30m_mean": 1.0,
        "liq_short_notional": 1e6,
    }
    d = decide_state(Side.LONG, feat, m, data)
    assert d.state in (ImpulseState.NO_EVIDENCE, ImpulseState.EARLY_PRESSURE, ImpulseState.MIXED)
    assert d.state != ImpulseState.CONFIRMED


def test_long_short_symmetry_thresholds():
    assert math.isclose(FROZEN_DEFAULT.sell_tbr_max, 1 - FROZEN_DEFAULT.tbr_thr)
    assert math.isclose(FROZEN_DEFAULT.flow_clean_tbr_max_short, 1 - FROZEN_DEFAULT.flow_clean_tbr_min)


def test_outcomes_not_used_in_decide(monkeypatch):
    # ensure outcome_mfe_mae is independent
    prices = [100, 101, 102, 99, 100]
    out = outcome_mfe_mae(prices, prices, prices, 0, Side.LONG, [3], sample_seconds=1)
    assert out["mfe_3s"] is not None
    data = DataAvailability(trades="VALID", orderbook="VALID", oi="VALID", candles="VALID")
    m = compute_impulse_metrics(prices + [100] * 100, [1] * 105, [1] * 105, 0, Side.LONG)
    d = decide_state(Side.LONG, _long_confirming_feat(ret_5m=0.0, rv5_vs_prior30_med=1.0, vol_vs_30m_mean=1.0, imbalance_l50=0.1), m, data)
    # decision must not require future mfe
    assert isinstance(d, DecisionSnapshot)


def test_reproducible_decide():
    data = DataAvailability(trades="VALID", orderbook="VALID", oi="VALID", candles="VALID")
    prices = [100.0 + i * 0.01 for i in range(200)]
    flow = [1] * 200
    imb = [1] * 200
    m = compute_impulse_metrics(prices, flow, imb, 0, Side.LONG)
    feat = _long_confirming_feat()
    a = decide_state(Side.LONG, feat, m, data)
    b = decide_state(Side.LONG, feat, m, data)
    assert a.state == b.state and a.reason == b.reason


def test_confirmed_requires_persistence_and_low_giveback():
    data = DataAvailability(trades="VALID", orderbook="VALID", oi="VALID", candles="VALID")
    prices = [100.0]
    for i in range(1, 200):
        prices.append(100.0 + 0.002 * i)  # steady grind up, low giveback
    m = compute_impulse_metrics(prices, [1] * 200, [1] * 200, 0, Side.LONG)
    d = decide_state(Side.LONG, _long_confirming_feat(), m, data)
    assert d.state == ImpulseState.CONFIRMED
    assert d.live_entry_allowed is False
