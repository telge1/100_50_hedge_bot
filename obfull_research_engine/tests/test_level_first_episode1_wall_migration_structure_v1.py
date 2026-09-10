"""Tests for wall migration structure (epoch-isolated)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

import pytest

from obfull_research_engine.level_first_episode1_wall_migration_structure_v1 import (  # noqa: E402
    ASK_WALL_BREACH,
    BAND_CONTRACT,
    LAYER_POST_BREACH,
    LAYER_POST_EPOCH,
    LAYER_PRE_EXISTING,
    WALL_MIGRATION_STATUS,
)
from obfull_research_engine.level_first_episode1_wall_migration_structure_v1.generations import (  # noqa: E402
    AskWallGeneration,
    _gid,
)
from obfull_research_engine.level_first_episode1_wall_migration_structure_v1.migration_proxy import (  # noqa: E402
    build_migration_proxies,
)
from obfull_research_engine.level_first_episode1_wall_migration_structure_v1.oracle import (  # noqa: E402
    oracle_audit,
)
from obfull_research_engine.level_first_episode1_wall_migration_structure_v1.pipeline import (  # noqa: E402
    run_wall_migration_structure,
)
from obfull_research_engine.drilldown.aggregation_100ms import _as_dt  # noqa: E402
from datetime import datetime, timezone


def _gen(**kw):
    base = dict(
        wall_generation_id="wg_x",
        replay_epoch=4,
        side="ask",
        price=79780.0,
        start_time="2026-09-06T20:19:10Z",
        end_time="2026-09-06T20:19:40Z",
        initial_qty=10.0,
        peak_qty=12.0,
        final_qty=0.0,
        cumulative_attributed_fills=8.0,
        cumulative_residual_pulls=2.0,
        cumulative_refills=0.0,
        termination_reason="MOSTLY_FILLED",
        first_trade_touch=None,
        last_trade_touch=None,
        distance_from_original_wall_ticks=0.0,
        distance_from_mid_ticks=1.0,
        attribution_confidence="HIGH",
        coverage_ok=True,
        layer_class=LAYER_PRE_EXISTING,
        generation_index=1,
    )
    base.update(kw)
    return AskWallGeneration(**base)


def test_01_band_contract_outcome_blind():
    assert BAND_CONTRACT["ticks_above"] == 500
    assert BAND_CONTRACT["band_high"] == 79780.0 + 500 * 0.1
    assert "Not optimized" in BAND_CONTRACT["selection_rule"]


def test_02_same_price_different_epochs_different_ids():
    t = datetime(2026, 9, 6, 20, 19, 10, tzinfo=timezone.utc)
    a = _gid(side="ask", price=79780.0, epoch=4, gen_index=1, start=t)
    b = _gid(side="ask", price=79780.0, epoch=5, gen_index=1, start=t)
    assert a != b


def test_03_no_proxy_across_epochs():
    g4 = _gen(wall_generation_id="wg_a", replay_epoch=4, end_time="2026-09-06T20:19:40Z")
    g5 = _gen(
        wall_generation_id="wg_b",
        replay_epoch=5,
        start_time="2026-09-06T20:20:00Z",
        end_time="2026-09-06T20:20:10Z",
        layer_class=LAYER_POST_EPOCH,
    )
    proxies = build_migration_proxies([g4, g5], breach_iso=ASK_WALL_BREACH, replay_epoch=4)
    assert all(p["replay_epoch"] == 4 for p in proxies)
    assert all(p["next_wall_generation_id"] != "wg_b" for p in proxies)
    assert all(p["wall_migration_status"] == WALL_MIGRATION_STATUS for p in proxies)


def test_04_preexisting_vs_post_breach_oracle():
    gens4 = [
        _gen(
            wall_generation_id="wg_pre",
            start_time="2026-09-06T20:19:10Z",
            end_time="2026-09-06T20:19:41.872Z",
            layer_class=LAYER_PRE_EXISTING,
            price=79785.0,
        ).to_dict(),
        _gen(
            wall_generation_id="wg_post",
            start_time="2026-09-06T20:19:50Z",
            end_time="2026-09-06T20:19:55Z",
            layer_class=LAYER_POST_BREACH,
            price=79790.0,
            generation_index=2,
        ).to_dict(),
    ]
    gens5 = [
        _gen(
            wall_generation_id="wg_ep5",
            replay_epoch=5,
            start_time="2026-09-06T20:20:00Z",
            end_time=None,
            layer_class=LAYER_POST_EPOCH,
            generation_index=1,
        ).to_dict()
    ]
    aud = oracle_audit(
        gens_ep4=gens4,
        gens_ep5=gens5,
        liquidity_rows=[{"total_ask_qty": 1.0, "quantity_above_original_wall": 0.5, "quantity_at_original_wall": 0.5}],
        proxies=[],
        breach_iso=ASK_WALL_BREACH,
    )
    assert aud["ok"] is True
    assert aud["fp"] == 0 and aud["fn"] == 0


def test_05_wrong_layer_flagged():
    gens4 = [
        _gen(
            wall_generation_id="wg_bad",
            start_time="2026-09-06T20:19:10Z",
            layer_class=LAYER_POST_BREACH,  # wrong — before breach
        ).to_dict()
    ]
    aud = oracle_audit(
        gens_ep4=gens4,
        gens_ep5=[],
        liquidity_rows=[],
        proxies=[],
        breach_iso=ASK_WALL_BREACH,
    )
    assert aud["fn"] >= 1


def test_06_future_gens_do_not_change_earlier_proxy_set():
    early = [
        _gen(wall_generation_id="wg1", end_time="2026-09-06T20:19:20Z", price=79780.0),
        _gen(
            wall_generation_id="wg2",
            start_time="2026-09-06T20:19:21Z",
            end_time="2026-09-06T20:19:30Z",
            price=79781.0,
            generation_index=2,
        ),
    ]
    p1 = build_migration_proxies(early, breach_iso=ASK_WALL_BREACH, replay_epoch=4)
    later = list(early) + [
        _gen(
            wall_generation_id="wg_future",
            start_time="2026-09-06T20:19:50Z",
            end_time="2026-09-06T20:19:55Z",
            price=79800.0,
            generation_index=3,
            layer_class=LAYER_POST_BREACH,
        )
    ]
    p2 = build_migration_proxies(later, breach_iso=ASK_WALL_BREACH, replay_epoch=4)
    # First proxy pair should remain identical
    assert p1[0]["previous_wall_generation_id"] == p2[0]["previous_wall_generation_id"]
    assert p1[0]["next_wall_generation_id"] == p2[0]["next_wall_generation_id"]
    assert p1[0]["price_shift_ticks"] == p2[0]["price_shift_ticks"]


def test_07_fills_pulls_not_negative_in_oracle():
    bad = _gen(cumulative_attributed_fills=-1.0).to_dict()
    aud = oracle_audit(
        gens_ep4=[bad],
        gens_ep5=[],
        liquidity_rows=[],
        proxies=[],
        breach_iso=ASK_WALL_BREACH,
    )
    assert aud["mass_error"] >= 1


@pytest.mark.slow
def test_08_pipeline_smoke(tmp_path):
    r = run_wall_migration_structure(run_key="wms_smoke", out_dir=tmp_path / "smoke")
    assert r["oracle"]["ok"] is True
    assert r["oracle"]["fp"] == 0 and r["oracle"]["fn"] == 0
    assert r["n_gens_ep4"] >= 1
    # no dwell/migration status calibrated
    assert r["wall_migration_status"] == WALL_MIGRATION_STATUS
