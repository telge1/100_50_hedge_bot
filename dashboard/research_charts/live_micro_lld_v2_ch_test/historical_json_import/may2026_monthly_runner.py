#!/usr/bin/env python3
"""
May 2026 four-combo monthly runner (nohup-ready).

Phase 4W: use ``--mode dry_run`` only. Production writes require explicit approval + env gates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_DASH = Path(__file__).resolve().parents[3]
_REPO = _DASH.parents[2]
for _p in (_REPO, _DASH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_sequence import (
    ControlledMonthlySequenceRunner,
    load_monthly_sequence_plan,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.post_import_verification import (
    verify_manifest_file,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.productive_prewrite_gate import (
    ProductivePrewriteGateError,
    assert_productive_env_gates,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.resource_limit_proposal import (
    proposed_500_batch_limit_environ,
)

DEFAULT_LOG_ROOT = _REPO / "logs" / "historical_import" / "may2026"
DEFAULT_PLAN = (
    _DASH
    / "research_charts/live_micro_lld_v2_ch_test/historical_json_import/manifests"
    / "phase4w_may2026_monthly_run_plan_v1.json"
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="May 2026 controlled monthly sequence runner")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument(
        "--mode",
        choices=("dry_run", "write_and_readback"),
        default="dry_run",
    )
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--run-id", default="may2026_monthly")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Resume/run state JSON (default: <log-root>/monthly_runner_state.json)",
    )
    parser.add_argument(
        "--progress-dir",
        type=Path,
        default=None,
        help="Orchestrator progress root (default: catalog/_controlled_import_progress_may2026)",
    )
    args = parser.parse_args(argv)

    log_root = args.log_root
    log_root.mkdir(parents=True, exist_ok=True)
    pid_file = log_root / "monthly_runner.pid"
    exit_file = log_root / "monthly_runner.exit"
    state_file = args.state_file or (log_root / "monthly_runner_state.json")
    main_log = log_root / "monthly_runner.log"

    if exit_file.is_file():
        exit_file.unlink()

    if pid_file.is_file():
        try:
            old_pid = int(pid_file.read_text().strip())
            if old_pid > 0 and Path(f"/proc/{old_pid}").exists():
                print(f"runner already active pid={old_pid}", file=sys.stderr)
                return 9
        except ValueError:
            pass

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    started = _utc_iso()
    t0 = time.monotonic()
    exit_code = 0
    result_payload: dict = {
        "started_at": started,
        "mode": args.mode,
        "plan": str(args.plan),
        "run_id": args.run_id,
        "pid": os.getpid(),
    }

    try:
        plan = load_monthly_sequence_plan(args.plan)
        if args.mode == "write_and_readback":
            assert_productive_env_gates()
            verified = os.environ.get("HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID", "")
            if verified != plan.backup_recovery_id:
                raise ProductivePrewriteGateError(
                    "HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID "
                    f"({verified}) != plan ({plan.backup_recovery_id})"
                )
            for key, val in proposed_500_batch_limit_environ().items():
                if os.environ.get(key) != val:
                    raise ProductivePrewriteGateError(
                        f"resource env {key} must be {val} for monthly write"
                    )

        progress = args.progress_dir or (CATALOG_ROOT / "_controlled_import_progress_may2026")
        runner = ControlledMonthlySequenceRunner(CATALOG_ROOT, progress_dir=progress)
        seq_res = runner.run(
            plan,
            mode=args.mode,
            resume_state_path=state_file,
        )
        result_payload["sequence"] = {
            "status": seq_res.status,
            "stopped_at_combo": seq_res.stopped_at_combo,
            "stopped_reason": seq_res.stopped_reason,
            "errors": seq_res.errors,
            "steps": seq_res.steps,
        }

        if args.mode == "write_and_readback" and seq_res.status == "readback_ok":
            final_rb: dict = {"combos": []}
            for step in plan.steps:
                mpath = Path(step.manifest_path)
                final_rb["combos"].append(
                    verify_manifest_file(CATALOG_ROOT, mpath, test_run_id=step.manifest.run_id)
                )
            _write_json(log_root / "final_readback.json", final_rb)
            result_payload["final_readback_ok"] = all(c.get("ok") for c in final_rb["combos"])

        if seq_res.status not in ("dry_run_ok", "readback_ok"):
            exit_code = 1
    except Exception as exc:  # noqa: BLE001 — runner must record exit for nohup
        result_payload["exception"] = repr(exc)
        exit_code = 1
    finally:
        result_payload["finished_at"] = _utc_iso()
        result_payload["wall_seconds"] = time.monotonic() - t0
        result_payload["exit_code"] = exit_code
        _write_json(state_file, result_payload)
        exit_file.write_text(str(exit_code), encoding="utf-8")
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    with main_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result_payload) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
