"""Tests for APT multi-start validation helpers and invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.backtests.cobertura_0_notional_strategie.config import default_apt_example
from research.backtests.cobertura_0_notional_strategie.multistart_seeding import (
    REFERENCE_START_TS,
    build_reference_absolute_seed,
    build_relative_core_seed,
    materialize_start,
    reference_geometry,
    select_start_indices,
)
from research.backtests.cobertura_0_notional_strategie.run_apt_multistart_validation import (
    NET_BE_SAFETY_BUFFER_USDT,
    NET_BE_TARGET_USDT,
    build_run_cfg,
    extract_run_metrics,
)
from research.backtests.cobertura_0_notional_strategie.run_net_be_policy_comparison import (
    _policy_specs,
)
from research.backtests.cobertura_0_notional_strategie.runner import run_cobertura


def _ts(i: int) -> datetime:
    return datetime(2025, 12, 27, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _candles(n: int) -> list[dict]:
    out = []
    for i in range(n):
        px = 1.5
        out.append(
            {
                "timestamp": _ts(i),
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "volume": 1.0,
            }
        )
    return out


def test_select_starts_deterministic_and_includes_reference():
    # Build candles covering reference timestamp
    ref = datetime(2026, 1, 19, 3, 55, tzinfo=timezone.utc)
    base = datetime(2025, 12, 27, 0, 0, tzinfo=timezone.utc)
    # index of ref
    delta_min = int((ref - base).total_seconds() // 60)
    assert delta_min % 5 == 0
    ref_i = delta_min // 5
    n = ref_i + 40 * 288  # ref + 40 days forward
    candles = []
    for i in range(n):
        ts = base + timedelta(minutes=5 * i)
        candles.append(
            {
                "timestamp": ts,
                "open": 1.6,
                "high": 1.6,
                "low": 1.6,
                "close": 1.6,
                "volume": 1.0,
            }
        )
    a = select_start_indices(candles, spacing_hours=24, min_forward_days=30)
    b = select_start_indices(candles, spacing_hours=24, min_forward_days=30)
    assert a == b
    assert ref_i in a
    assert len(a) >= 1


def test_relative_seed_preserves_locked_loss_and_notionals():
    geom = reference_geometry()
    cfg = default_apt_example()
    for sp in (1.2, 1.6456, 2.0, 0.8):
        seed = build_relative_core_seed(
            start_price=sp, qty_step=cfg.qty_step, tick_size=cfg.tick_size
        )
        locked = seed["core_long_qty"] * (
            seed["core_long_avg"] - seed["core_short_avg"]
        )
        long_n = seed["core_long_qty"] * seed["core_long_avg"]
        short_n = seed["core_short_qty"] * seed["core_short_avg"]
        assert locked == pytest.approx(geom.locked_loss_usdt, rel=2e-3, abs=0.15)
        assert long_n == pytest.approx(geom.long_notional_usdt, rel=2e-3, abs=0.15)
        assert short_n == pytest.approx(geom.short_notional_usdt, rel=2e-3, abs=0.15)
        assert seed["core_long_qty"] == seed["core_short_qty"]


def test_reference_absolute_seed_matches_apt_example():
    cfg = default_apt_example()
    seed = build_reference_absolute_seed(cfg)
    assert seed["start_price"] == cfg.start_price
    assert seed["core_long_avg"] == cfg.core_long_avg
    assert seed["core_short_avg"] == cfg.core_short_avg
    assert seed["core_long_qty"] == cfg.core_long_qty


def test_same_starts_for_all_policies_run_ids():
    candles = _candles(50 * 288)
    idxs = select_start_indices(candles, spacing_hours=24, min_forward_days=30, max_starts=5)
    assert len(idxs) >= 1
    policies = [p["run_id"] for p in _policy_specs()]
    # Each start pairs with each policy → unique run ids, same start set
    starts = {i for i in idxs}
    assert len(starts) == len(idxs)
    assert len(policies) == 3


def test_horizon_and_min_forward_eligibility():
    candles = _candles(20 * 288)  # only 20 days
    idxs = select_start_indices(candles, spacing_hours=24, min_forward_days=30)
    # Need 30 forward days → no eligible starts in 20-day series (except maybe none)
    assert idxs == []


def test_build_run_cfg_net_be_and_isolation_fields():
    base = default_apt_example()
    candles = _candles(40 * 288)
    idxs = select_start_indices(candles, spacing_hours=24, min_forward_days=30, max_starts=1)
    seed = materialize_start(candles, idxs[0], cfg_template=base)
    policy = _policy_specs()[0]
    cfg = build_run_cfg(
        base=base,
        policy=policy,
        seed=seed,
        end_timestamp=candles[min(idxs[0] + 10, len(candles) - 1)]["timestamp"].isoformat(),
        run_id="test_run",
    )
    assert cfg.full_exit_target_mode == "net_be"
    assert cfg.full_exit_target_usdt == NET_BE_TARGET_USDT
    assert cfg.full_exit_safety_buffer_usdt == NET_BE_SAFETY_BUFFER_USDT
    assert cfg.candle_limit is None
    assert cfg.core_long_qty == seed.core_long_qty


def test_audited_reference_policies_still_fingerprint():
    """Existing audited APT net-BE outcomes must remain stable."""
    from research.backtests.cobertura_0_notional_strategie.metrics import (
        compute_policy_metrics,
    )

    base = default_apt_example()
    # shared_be legacy fingerprint
    cfg = default_apt_example()
    cfg.overlay_exit_policy = "shared_be"
    r = run_cobertura(cfg, write_outputs=False)
    m = compute_policy_metrics(r)
    assert m["final_status"] == "RECOVERED"
    assert m["final_total_economics_usdt"] == pytest.approx(30.596847805021635, rel=1e-6)

    # net_be audited representative (target 0 + buffer 0.25)
    for policy in _policy_specs():
        raw = base.to_dict()
        raw.update(policy)
        raw["full_exit_target_mode"] = "net_be"
        raw["full_exit_target_usdt"] = 0.0
        raw["full_exit_safety_buffer_usdt"] = 0.25
        raw["run_id"] = f"fp_{policy['run_id']}"
        from research.backtests.cobertura_0_notional_strategie.config import (
            CoberturaConfig,
        )

        cfg = CoberturaConfig.from_dict(raw)
        result = run_cobertura(cfg, write_outputs=False)
        assert result.state == "RECOVERED_BE"
        assert result.ledger.net_qty() == pytest.approx(0.0, abs=1e-9)


def test_isolated_state_between_two_starts():
    """Two consecutive runs must not share ledger mutation."""
    base = default_apt_example()
    candles = None  # load inside
    from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol

    candles = load_candles_for_symbol(
        base.symbol, timeframe=base.timeframe, data_dir=DEFAULT_DATA_DIR, limit=None
    )
    idxs = select_start_indices(
        candles, spacing_hours=24, min_forward_days=30, max_starts=2
    )
    assert len(idxs) >= 2
    policy = _policy_specs()[1]
    results = []
    for i in idxs[:2]:
        seed = materialize_start(candles, i, cfg_template=base)
        end_i = min(i + 60 * 288, len(candles) - 1)
        end_ts = candles[end_i]["timestamp"]
        if hasattr(end_ts, "isoformat"):
            end_ts = end_ts.isoformat()
        cfg = build_run_cfg(
            base=base,
            policy=policy,
            seed=seed,
            end_timestamp=str(end_ts),
            run_id=f"iso_{i}",
        )
        results.append(run_cobertura(cfg, candles=candles, write_outputs=False))
    # Different starts → independent fill counts / states (not equal configs)
    assert results[0].cfg.start_timestamp != results[1].cfg.start_timestamp
    assert results[0].ledger is not results[1].ledger
