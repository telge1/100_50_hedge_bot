#!/usr/bin/env python3
"""Phase 4X-AQ binding tests (no CH writes, no 4X-AO execute)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AO_TOOL = ROOT / "tools/phase4xao_jun_sep_nohup_rollout_v1"
POOLS_DASH = ROOT / "backtester/short/dashboard"
RESULTS = ROOT / "results/phase4xaq_september_safe_snapshot_binding_v1"

OLD_ID = "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b"
NEW_ID = "6c2e01d76a1559a94ec373abdb4b96bc73f3ddaba89bd1ee1058b001d04d6d61"
NEW_HASH = "62a2a808b05cf98e2c16f10b1aad2712be0e6e4c984242396f4a6a47d54e65fb"
BINDING_ENV = "JUN_SEP_SEPTEMBER_IMMUTABLE_BINDING_V1"


def _setup() -> None:
    for p in (ROOT, Path("/home/telgenbuescher/projects/trading_research_platform"), POOLS_DASH, AO_TOOL):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def run_tests() -> dict:
    _setup()
    from september_immutable_binding_v1 import (
        BINDING_ENV as ENV_NAME,
        ImmutableBindingError,
        binding_env_enabled,
        load_session_for_month,
        resolve_immutable_input,
    )
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
        build_active_state_at_decision_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import verification_record
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
        HISTORY_POLICY_ROLLING_4M,
        rolling_history_from_close,
    )

    out: dict = {"cases": {}, "verdict": "READY_FOR_CONTROLLED_SEPTEMBER_RESUME_APPROVAL"}

    os.environ.pop(ENV_NAME, None)
    aug = resolve_immutable_input("2026-08")
    out["cases"]["august_default_no_env"] = {
        "ok": aug.binding_mode == "default" and aug.snapshot_id == OLD_ID,
        "mode": aug.binding_mode,
        "snapshot_id": aug.snapshot_id,
    }

    sep_def = resolve_immutable_input("2026-09")
    out["cases"]["september_default_no_env"] = {
        "ok": sep_def.binding_mode == "default" and sep_def.snapshot_id == OLD_ID,
        "mode": sep_def.binding_mode,
        "snapshot_id": sep_def.snapshot_id,
    }

    os.environ[ENV_NAME] = "1"
    sep_pin = resolve_immutable_input("2026-09")
    aug2 = resolve_immutable_input("2026-08")
    out["cases"]["september_with_env_pinned"] = {
        "ok": sep_pin.binding_mode == "september_pinned_v1" and sep_pin.snapshot_id == NEW_ID,
        "mode": sep_pin.binding_mode,
        "snapshot_id": sep_pin.snapshot_id,
        "hash": sep_pin.ohlcv_content_hash,
    }
    out["cases"]["august_with_env_still_default"] = {
        "ok": aug2.binding_mode == "default" and aug2.snapshot_id == OLD_ID,
        "snapshot_id": aug2.snapshot_id,
    }

    bad_hash = False
    spec_path = AO_TOOL / "september_immutable_binding_v1.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    backup = spec.copy()
    try:
        spec["ohlcv_content_hash"] = "0" * 64
        spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        try:
            resolve_immutable_input("2026-09")
        except ImmutableBindingError as e:
            bad_hash = e.code == "BINDING_OHLCV_HASH_MISMATCH"
    finally:
        spec_path.write_text(json.dumps(backup, indent=2), encoding="utf-8")
    out["cases"]["wrong_hash_fail_closed"] = {"ok": bad_hash}

    incomplete = False
    try:
        spec2 = backup.copy()
        spec2["expected_row_count"] = 1
        spec_path.write_text(json.dumps(spec2, indent=2), encoding="utf-8")
        try:
            resolve_immutable_input("2026-09")
        except ImmutableBindingError as e:
            incomplete = e.code in ("BINDING_ROW_COUNT_MISMATCH", "BINDING_COVERAGE_INCOMPLETE")
    finally:
        spec_path.write_text(json.dumps(backup, indent=2), encoding="utf-8")
    out["cases"]["incomplete_snapshot_fail_closed"] = {"ok": incomplete}

    os.environ[ENV_NAME] = "1"
    session, identity, _ = load_session_for_month("2026-09")
    boundary_ok = True
    boundary = {}
    for iso in (
        "2026-08-31T23:55:00Z",
        "2026-09-01T00:00:00Z",
        "2026-09-01T00:05:00Z",
        "2026-09-12T12:00:00Z",
        "2026-09-30T23:55:00Z",
    ):
        dc = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        hist = rolling_history_from_close(dc, 4)
        try:
            snap, candles, result = build_active_state_at_decision_close(
                "DOGEUSDT",
                "5m",
                hist,
                dc,
                pool_detail=1,
                history_policy=HISTORY_POLICY_ROLLING_4M,
                candle_session=session,
            )
            boundary[iso] = {"ok": True, "active": len(snap.active_pool_ids)}
        except Exception as exc:
            boundary_ok = False
            boundary[iso] = {"ok": False, "error": str(exc)}
    out["cases"]["boundary_five"] = {"ok": boundary_ok, "detail": boundary}

    LMV2 = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test"
    sep_frozen = (
        LMV2 / "doge_multi_month_2026_history/staging/phase6xt_5m_pd1_jan_sep_v1/results/2026-09/5m_PD1"
    )
    parity_ok = True
    parity = {}
    for stem in (
        "20260901T000000Z",
        "20260909T000000Z",
        "20260912T120000Z",
        "20260917T235500Z",
        "20260930T235500Z",
    ):
        path = sep_frozen / f"{stem}.json"
        frozen = json.loads(path.read_text(encoding="utf-8"))
        dc = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        hist = rolling_history_from_close(dc, 4)
        snap, candles, result = build_active_state_at_decision_close(
            "DOGEUSDT",
            "5m",
            hist,
            dc,
            pool_detail=1,
            history_policy=HISTORY_POLICY_ROLLING_4M,
            candle_session=session,
        )
        rec = verification_record(snap, candles, result, dc)
        ok = (
            frozen.get("engine_input_hash") == rec.get("engine_input_hash")
            and frozen.get("active_state_fingerprint") == rec.get("active_state_fingerprint")
            and sorted(frozen.get("active_pool_ids") or []) == sorted(snap.active_pool_ids)
        )
        parity[stem] = ok
        parity_ok = parity_ok and ok
    out["cases"]["parity_five"] = {"ok": parity_ok, "detail": parity}

    from run_jun_sep_nohup import _running_lock

    runners = _running_lock()
    out["cases"]["no_concurrent_runner_at_test_time"] = {
        "ok": isinstance(runners, list) and len(runners) == 0,
        "active_runners": runners,
    }

    sys.path.insert(0, str(AO_TOOL))
    from import_path_5m_pd1 import setup_hcfg
    from run_jun_sep_nohup import ch_ledger

    hcfg = setup_hcfg()
    ledger = ch_ledger(hcfg)
    sep_win = int(ledger.get("sep_window", -1))
    out["cases"]["sep_pilot_ledger_2592"] = {"ok": sep_win == 2592, "count": sep_win}

    os.environ.pop(ENV_NAME, None)
    if not all(c.get("ok") for c in out["cases"].values()):
        failed = [k for k, v in out["cases"].items() if not v.get("ok")]
        out["failed"] = failed
        if any("hash" in k or "incomplete" in k for k in failed):
            out["verdict"] = "BLOCKED_BINDING"
        elif "parity" in str(failed) or "boundary" in str(failed):
            out["verdict"] = "BLOCKED_TESTS"
        else:
            out["verdict"] = "BLOCKED_TESTS"
    return out


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    report = run_tests()
    (RESULTS / "test_results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report.get("verdict") == "READY_FOR_CONTROLLED_SEPTEMBER_RESUME_APPROVAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
