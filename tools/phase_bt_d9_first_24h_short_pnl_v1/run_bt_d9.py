#!/usr/bin/env python3
"""BT-D9: first complete 24h Depth2 short PnL backtest orchestration."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
OUT = ROOT / "results/phase_bt_d9_first_24h_short_pnl_v1"
CFG_PATH = TOOLS / "configs/bt_d9_24h_v1.json"
DONOR_BUNDLE = (
    ROOT
    / "results/phase_bt_d8_canonical_density_cache_v1/bundles/dogeusdt_5m_pd1_depth2_pilot_2h_20260911T1200_1400Z"
)
BUNDLE_DIR = OUT / "cache_bundle"
PID_FILE = OUT / "pipeline.pid"
STATUS_FILE = OUT / "pipeline.status.json"
LOG_FILE = OUT / "pipeline.log"
EXIT_FILE = OUT / "pipeline.exitcode"

BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D8 = ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"
for p in (str(TOOLS), str(BT_D6), str(BT_D7), str(BT_D8), str(ROOT / "tools/phase4d_pool_landscape_backtester_v2")):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_validate_v1 import load_and_validate_config  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from experiment_v1 import prepare_experiment  # noqa: E402
from manifest_v1 import config_content_hash  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from short_research_engine_v1 import run_short_research_backtest  # noqa: E402

from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402
from cache_store_v1 import load_bundle_manifest  # noqa: E402
from cache_build_24h_v1 import build_24h_bundle_incremental  # noqa: E402
from pnl_report_v1 import format_pnl_summary  # noqa: E402
from preflight_v1 import run_preflight  # noqa: E402
from window_v1 import iter_5m_closes_half_open  # noqa: E402

START_UTC = "2026-09-11T12:00:00Z"
END_UTC_EXCLUSIVE = "2026-09-12T12:00:00Z"


def _required_closes() -> list[str]:
    return iter_5m_closes_half_open(START_UTC, END_UTC_EXCLUSIVE)


def _write_status(payload: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payload["updated_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_lock() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if PID_FILE.is_file():
        try:
            old = int(PID_FILE.read_text(encoding="utf-8").strip())
            if _pid_alive(old):
                raise SystemExit(f"another BT-D9 pipeline running (pid {old})")
        except ValueError:
            pass
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _release_lock() -> None:
    PID_FILE.unlink(missing_ok=True)


def cmd_preflight(args: argparse.Namespace) -> dict[str, Any]:
    require_cache = args.require_cache
    return run_preflight(CFG_PATH, required_closes=_required_closes(), require_complete_cache=require_cache)


def cmd_build_cache(_: argparse.Namespace) -> dict[str, Any]:
    if BUNDLE_DIR.exists():
        return {"skipped": True, "reason": "BUNDLE_EXISTS", "path": str(BUNDLE_DIR)}
    closes = _required_closes()
    donors = [DONOR_BUNDLE] if DONOR_BUNDLE.is_dir() else []

    def progress(i: int, total: int, close: str, kind: str) -> None:
        _write_status({"phase": "cache_build", "progress": i, "total": total, "close": close, "kind": kind})

    _write_status({"phase": "cache_build", "state": "started"})
    t0 = time.perf_counter()
    stats = build_24h_bundle_incremental(
        bundle_dir=BUNDLE_DIR,
        decision_closes=closes,
        donor_bundle_dirs=donors,
        progress_cb=progress,
    )
    stats["wall_s"] = round(time.perf_counter() - t0, 2)
    _write_status({"phase": "cache_build", "state": "completed", **stats})
    return stats


def cmd_verify_cache(_: argparse.Namespace) -> dict[str, Any]:
    manifest = load_bundle_manifest(BUNDLE_DIR)
    closes = _required_closes()
    have = {e["decision_close_utc"] for e in manifest.get("entries") or []}
    missing = [c for c in closes if c not in have]
    session = ValidatedDensityCacheSessionV1(BUNDLE_DIR)
    sample = closes[0]
    snap = session.collect_at_close(sample)
    readback = {"ok": len(missing) == 0, "missing_count": len(missing), "sample_close": sample}
    return {"completeness": readback, "manifest_entry_count": manifest.get("entry_count"), "sample_bands": snap.get("total_band_count")}


def cmd_run_backtest(_: argparse.Namespace) -> dict[str, Any]:
    cfg = load_and_validate_config(CFG_PATH)
    prices = load_prices_from_ssot_window(
        cfg["backtest"]["start_utc"],
        cfg["backtest"]["end_utc"],
        end_utc_exclusive=bool(cfg["backtest"].get("end_utc_exclusive")),
    )
    session = create_density_session(cfg)
    t0 = time.perf_counter()
    result = run_short_research_backtest(cfg, session, prices)
    wall = round(time.perf_counter() - t0, 2)
    exp = prepare_experiment(cfg, "bt_d9_24h_run", source_config_path=CFG_PATH)
    (exp / "signals.json").write_text(json.dumps(result["signals"], indent=2), encoding="utf-8")
    (exp / "orders.json").write_text(json.dumps(result["orders"], indent=2), encoding="utf-8")
    (exp / "trades.json").write_text(json.dumps(result["trades"], indent=2), encoding="utf-8")
    pnl_summary = format_pnl_summary(result.get("pnl"))
    (exp / "pnl_summary.json").write_text(json.dumps(pnl_summary, indent=2), encoding="utf-8")
    (exp / "integration_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    manifest = load_bundle_manifest(BUNDLE_DIR)
    out = {
        "experiment_dir": str(exp),
        "config_hash": config_content_hash(cfg),
        "strategy_label": "EXPERIMENTAL_NOT_FINAL_APPROVED",
        "tp_mode": "next_eligible_band",
        "signals": len(result["signals"]),
        "orders": len(result["orders"]),
        "trades": len(result["trades"]),
        "pnl": pnl_summary,
        "density_provenance": {
            "bundle": str(BUNDLE_DIR),
            "pipeline_line_id": manifest.get("required_pipeline_line_id"),
            "config_hash": manifest.get("required_config_hash"),
            "entry_count": manifest.get("entry_count"),
        },
        "backtest_wall_s": wall,
    }
    (OUT / "backtest_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def cmd_pipeline(_: argparse.Namespace) -> int:
    _acquire_lock()
    code = 0
    try:
        pf = run_preflight(CFG_PATH, required_closes=_required_closes(), require_complete_cache=False)
        (OUT / "preflight_initial.json").write_text(json.dumps(pf, indent=2), encoding="utf-8")
        if pf["verdict"] == "BT_D9_24H_PNL_BLOCKED":
            _write_status({"phase": "preflight", "verdict": pf["verdict"], "blockers": pf["blockers"]})
            code = 2
            return code

        if not BUNDLE_DIR.is_dir():
            build_stats = cmd_build_cache(_)
            (OUT / "cache_build.json").write_text(json.dumps(build_stats, indent=2), encoding="utf-8")

        pf2 = run_preflight(CFG_PATH, required_closes=_required_closes(), require_complete_cache=True)
        (OUT / "preflight_final.json").write_text(json.dumps(pf2, indent=2), encoding="utf-8")
        if pf2["verdict"] == "BT_D9_24H_PNL_BLOCKED":
            _write_status({"phase": "preflight_final", "verdict": pf2["verdict"]})
            code = 2
            return code

        verify = cmd_verify_cache(_)
        (OUT / "cache_verify.json").write_text(json.dumps(verify, indent=2), encoding="utf-8")

        bt = cmd_run_backtest(_)
        verdict = "BT_D9_24H_PNL_PASS"
        _write_status({"phase": "complete", "verdict": verdict, "trades": bt["trades"]})
        (OUT / "final_verdict.json").write_text(json.dumps({"verdict": verdict, "backtest": bt}, indent=2), encoding="utf-8")
        from report_v1 import write_report  # noqa: E402

        write_report(OUT, ROOT / "reports/PHASE_BT_D9_FIRST_24H_SHORT_PNL_V1.md")
    except Exception as exc:  # noqa: BLE001
        _write_status({"phase": "error", "error": str(exc)})
        (OUT / "pipeline_error.json").write_text(json.dumps({"error": str(exc)}, indent=2), encoding="utf-8")
        code = 1
    finally:
        EXIT_FILE.write_text(str(code), encoding="utf-8")
        _release_lock()
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["preflight", "build-cache", "verify-cache", "run-backtest", "pipeline"],
        required=True,
    )
    parser.add_argument("--require-cache", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    if args.mode == "pipeline":
        return cmd_pipeline(args)
    handlers = {
        "preflight": cmd_preflight,
        "build-cache": cmd_build_cache,
        "verify-cache": cmd_verify_cache,
        "run-backtest": cmd_run_backtest,
    }
    result = handlers[args.mode](args)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
