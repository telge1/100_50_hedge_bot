"""Tests for APT Cobertura 2×2 handoff ablation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.backtests.cobertura_0_notional_strategie.config import CoberturaConfig
from research.backtests.cobertura_0_notional_strategie.run_apt_handoff_2x2_ablation import (
    EXPECTED_OPEN_0000,
    EXPECTED_OPEN_0355,
    FP_A,
    FP_D,
    HANDOFF_DIR,
    STRATEGY_CONSTANTS,
    STRATEGY_PARAM_KEYS,
    TS_0000,
    TS_0355,
    VARIANT_SPECS,
    build_variant_config,
    check_fingerprint,
    decide_ablation,
    historical_book_from_handoff,
    pairwise_delta,
    phase_a_book,
    run_ablation,
)

pytestmark = pytest.mark.skipif(
    not (HANDOFF_DIR / "handoff_state_after_neutralization.json").exists(),
    reason="handoff results missing",
)


def test_four_variants_and_param_identity(tmp_path: Path):
    out = run_ablation(output_dir=tmp_path / "ablation", write_variant_artifacts=False)
    assert len(out["variants"]) == 4
    ids = {r["variant_id"] for r in out["variants"]}
    assert ids == {s["variant_id"] for s in VARIANT_SPECS}
    cfgs: list[CoberturaConfig] = out["configs"]
    base = {k: getattr(cfgs[0], k) for k in STRATEGY_PARAM_KEYS}
    for cfg in cfgs[1:]:
        for k, v in base.items():
            assert getattr(cfg, k) == v
    # Only book + start differ.
    books = {
        (c.core_long_qty, c.core_long_avg, c.core_short_qty, c.core_short_avg)
        for c in cfgs
    }
    starts = {(c.start_timestamp, c.start_price) for c in cfgs}
    assert len(books) == 2
    assert len(starts) == 2


def test_start_prices_match_expected_candles(tmp_path: Path):
    out = run_ablation(output_dir=tmp_path / "ablation2", write_variant_artifacts=False)
    by = {r["variant_id"]: r for r in out["variants"]}
    assert by["historical_book_at_0000"]["start_price"] == pytest.approx(
        EXPECTED_OPEN_0000
    )
    assert by["historical_book_at_0355"]["start_price"] == pytest.approx(
        EXPECTED_OPEN_0355
    )
    assert by["phase_a_book_at_0000"]["start_timestamp"] == TS_0000
    assert by["phase_a_book_at_0355"]["start_timestamp"] == TS_0355
    # 00:00 open matches prescribed; 03:55 Phase-A uses config reference.
    assert by["historical_book_at_0000"]["start_price_equals_candle_open"] is True
    assert out["integrity"]["candle_open_0000"] == pytest.approx(EXPECTED_OPEN_0000)
    assert abs(out["integrity"]["candle_open_0355"] - EXPECTED_OPEN_0355) > 1e-9
    assert any("03:55" in w for w in out["integrity"]["warnings"])


def test_fingerprint_a_and_d(tmp_path: Path):
    out = run_ablation(output_dir=tmp_path / "ablation3", write_variant_artifacts=False)
    by = {r["variant_id"]: r for r in out["variants"]}
    assert not check_fingerprint(by["historical_book_at_0000"], FP_A)
    assert not check_fingerprint(by["phase_a_book_at_0355"], FP_D)
    assert out["integrity"]["fingerprint_a_ok"] is True
    assert out["integrity"]["fingerprint_d_ok"] is True


def test_qty_neutral_no_tem_no_initial_entry(tmp_path: Path):
    out = run_ablation(output_dir=tmp_path / "ablation4", write_variant_artifacts=False)
    for cfg in out["configs"]:
        assert abs(cfg.core_long_qty - cfg.core_short_qty) <= 1e-9
        assert cfg.tags.get("tem_orders_imported") is False
        assert cfg.tags.get("fresh_initial_entry_required") is False
    assert out["integrity"]["tem_orders_imported"] is False
    assert out["integrity"]["initial_entry_created"] is False


def test_pairwise_and_decision(tmp_path: Path):
    out = run_ablation(output_dir=tmp_path / "ablation5", write_variant_artifacts=False)
    pw = out["pairwise"]
    assert "start_time_on_historical_book_B_minus_A" in pw
    assert "book_effect_at_0355_D_minus_B" in pw
    a = next(r for r in out["variants"] if r["variant_id"] == "historical_book_at_0000")
    b = next(r for r in out["variants"] if r["variant_id"] == "historical_book_at_0355")
    delta = pairwise_delta(a, b, ["realized_overlay_pnl", "recovered"])
    assert delta["realized_overlay_pnl"]["delta_b_minus_a"] == pytest.approx(
        float(b["realized_overlay_pnl"]) - float(a["realized_overlay_pnl"])
    )
    assert out["decision"] != "APT_2X2_ABLATION_FAIL"


def test_refuse_overwrite(tmp_path: Path):
    root = tmp_path / "ablation6"
    run_ablation(output_dir=root, write_variant_artifacts=False)
    with pytest.raises(FileExistsError):
        run_ablation(output_dir=root, write_variant_artifacts=False)


def test_determinism(tmp_path: Path):
    r1 = run_ablation(output_dir=tmp_path / "d1", write_variant_artifacts=False)
    r2 = run_ablation(output_dir=tmp_path / "d2", write_variant_artifacts=False)
    for a, b in zip(r1["variants"], r2["variants"]):
        assert a["variant_id"] == b["variant_id"]
        assert a["final_state"] == b["final_state"]
        assert a["bars_processed"] == b["bars_processed"]
        assert a["realized_overlay_pnl"] == b["realized_overlay_pnl"]
        assert a["final_total_exit_economics"] == b["final_total_exit_economics"]


def test_books_loaded():
    h = historical_book_from_handoff()
    p = phase_a_book()
    assert h["core_long_qty"] == pytest.approx(296.365)
    assert p["core_long_qty"] == pytest.approx(395.153)
    assert abs(h["core_long_qty"] - h["core_short_qty"]) <= 1e-9
    assert abs(p["core_long_qty"] - p["core_short_qty"]) <= 1e-9


def test_build_variant_uses_strategy_constants():
    cfg = build_variant_config(
        variant_id="x",
        book=phase_a_book(),
        start_timestamp=TS_0355,
        start_price=EXPECTED_OPEN_0355,
        output_dir=Path("/tmp/x"),
    )
    for k, v in STRATEGY_CONSTANTS.items():
        assert getattr(cfg, k) == v


def test_decide_fail_on_fingerprint():
    fake = {
        "historical_book_at_0000": {"recovered": False, "realized_overlay_pnl": 0, "final_total_exit_economics": 0},
        "historical_book_at_0355": {"recovered": False, "realized_overlay_pnl": 0, "final_total_exit_economics": 0},
        "phase_a_book_at_0000": {"recovered": False, "realized_overlay_pnl": 0, "final_total_exit_economics": 0},
        "phase_a_book_at_0355": {"recovered": True, "realized_overlay_pnl": 1, "final_total_exit_economics": 1},
    }
    assert decide_ablation(fake, fp_a_ok=False, fp_d_ok=True) == "APT_2X2_ABLATION_FAIL"
