"""Tests for APT T3 economics diagnostic CLI mode."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from research.backtests import run_c4_undercoverage_fix_validation as mod


def test_parse_args_recognizes_dump_flag() -> None:
    args = mod.parse_args(
        [
            "--dump-apt-t3-economics",
            "--output-dir",
            "/tmp/c4_diag_out",
        ]
    )
    assert args.dump_apt_t3_economics is True
    assert Path(args.output_dir) == Path("/tmp/c4_diag_out")


def test_parse_args_default_is_full_revalidation() -> None:
    args = mod.parse_args([])
    assert args.dump_apt_t3_economics is False
    assert Path(args.output_dir) == mod.DEFAULT_OUT


def test_dump_mode_runs_only_apt_t3_two_early_medium(tmp_path: Path) -> None:
    calls: list[dict] = []

    class DummyResult:
        final_status = "closed"
        exit_reason = "flat_no_active_orders"
        realized_pnl = 1.25
        fill_log = [
            {
                "purpose": "LONG_TP_EXIT",
                "confirmed_closed_pnl": 1.0,
                "closed_pnl": 1.0,
            },
            {
                "purpose": "SHORT_SL_EXIT",
                "confirmed_closed_pnl": 0.25,
                "closed_pnl": 0.25,
            },
        ]

    def fake_run_isolated_blocker(**kwargs):
        calls.append(dict(kwargs))
        return DummyResult()

    capture = {
        "effective_pending_cycle_loss_usdt": 10.0,
        "target_profit_usdt": 2.0,
        "buffer_usdt": 1.0,
        "min_profit_target_usdt": 3.0,
        "tolerance_usdt": 0.15,
        "min_required_total_usdt": 13.0,
        "realized_cycle_net_usdt": 2.0,
        "stage0_realized_net_usdt": 2.0,
        "long_tp_gross_pnl_usdt": 12.0,
        "long_tp_fee_usdt": 1.0,
        "long_tp_net_pnl_usdt": 11.0,
        "short_sl_gross_pnl_usdt": -1.0,
        "short_sl_fee_usdt": 0.5,
        "short_sl_net_pnl_usdt": -1.5,
        "basket_gross_pnl_usdt": 11.0,
        "basket_fees_usdt": 1.5,
        "basket_net_usdt": 9.5,
        "expected_total_net_after_exit": 11.5,
        "target_delta_usdt": -1.5,
        "sufficient": True,  # -1.5 >= -0.15? False actually - fix below
        "reason_code": "coverage_ok_basket_compensates_partial_stages",
        "sources": {},
    }
    # Make sufficient consistent: delta -0.01, tol 0.15
    capture["target_delta_usdt"] = -0.01
    capture["expected_total_net_after_exit"] = 12.99
    capture["basket_net_usdt"] = 10.99
    capture["long_tp_net_pnl_usdt"] = 12.49
    capture["short_sl_net_pnl_usdt"] = -1.5
    # long+short nets = 10.99; realized 2 + basket 10.99 = 12.99; min_required 13; delta -0.01

    with mock.patch.object(mod, "run_isolated_blocker", side_effect=fake_run_isolated_blocker), mock.patch.object(
        mod, "normalize_candles", return_value=[]
    ), mock.patch.object(mod, "load_candles_for_symbol", return_value=[]), mock.patch.object(
        mod, "resolve_grid_profile", return_value=object()
    ), mock.patch.object(
        mod, "_capture_basket_close_economics", side_effect=lambda caps: caps.append(capture) or object()
    ), mock.patch.object(mod, "_restore_basket_coverage_method"):
        out = mod.dump_apt_t3_economics(output_dir=tmp_path)

    assert len(calls) == 1
    call = calls[0]
    assert call["coin"] == "APTUSDT"
    assert call["start_index"] == 570
    assert call["trade_number"] == 3
    assert out == tmp_path / mod.APT_T3_ECONOMICS_JSON
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["coin"] == "APTUSDT"
    assert payload["trade_number"] == 3
    assert payload["start_index"] == 570
    assert payload["profile"] == "two_early_medium"
    for name, identity in payload["identities"].items():
        assert identity.get("pass") is True, (name, identity)


def test_main_dump_flag_skips_full_revalidation(tmp_path: Path) -> None:
    with mock.patch.object(mod, "dump_apt_t3_economics") as dump_mock, mock.patch.object(
        mod, "run_full_revalidation"
    ) as full_mock:
        dump_mock.return_value = tmp_path / "apt_t3_economics_doublecheck.json"
        mod.main(["--dump-apt-t3-economics", "--output-dir", str(tmp_path)])
    dump_mock.assert_called_once()
    full_mock.assert_not_called()


def test_main_without_dump_keeps_full_revalidation(tmp_path: Path) -> None:
    with mock.patch.object(mod, "dump_apt_t3_economics") as dump_mock, mock.patch.object(
        mod, "run_full_revalidation"
    ) as full_mock:
        mod.main(["--output-dir", str(tmp_path)])
    full_mock.assert_called_once_with(output_dir=tmp_path)
    dump_mock.assert_not_called()


def test_live_dump_apt_t3_economics_identities(tmp_path: Path) -> None:
    out = mod.dump_apt_t3_economics(output_dir=tmp_path)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["profile"] == "two_early_medium"
    assert payload["coin"] == "APTUSDT"
    for name, identity in payload["identities"].items():
        if identity.get("available") is False:
            continue
        assert identity.get("pass") is True, (name, identity)
