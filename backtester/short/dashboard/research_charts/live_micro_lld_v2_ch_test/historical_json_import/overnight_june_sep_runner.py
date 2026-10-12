#!/usr/bin/env python3
"""Jun–Sep 2026 four-month overnight controlled import (nohup-ready)."""

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

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_overnight_sequence import (
    ControlledOvernightSequenceRunner,
    load_overnight_import_plan,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.productive_prewrite_gate import (
    ProductivePrewriteGateError,
    assert_productive_env_gates,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.resource_limit_proposal import (
    proposed_500_batch_limit_environ,
)

DEFAULT_LOG_ROOT = _REPO / "logs" / "historical_import" / "overnight_2026"
DEFAULT_PLAN = (
    _DASH
    / "research_charts/live_micro_lld_v2_ch_test/historical_json_import/manifests"
    / "june_sep_2026_overnight_import_plan_v1.json"
)
DEFAULT_PROGRESS = CATALOG_ROOT / "_controlled_import_progress_phase4z_overnight"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jun–Sep 2026 overnight import runner")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument(
        "--mode",
        choices=("dry_run", "write_and_readback"),
        default="dry_run",
    )
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Overnight resume state (default: <log-root>/overnight_state.json)",
    )
    parser.add_argument("--progress-root", type=Path, default=DEFAULT_PROGRESS)
    args = parser.parse_args(argv)

    log_root = args.log_root
    log_root.mkdir(parents=True, exist_ok=True)
    pid_file = log_root / "overnight.pid"
    exit_file = log_root / "overnight.exit"
    state_file = args.state_file or (log_root / "overnight_state.json")
    main_log = log_root / "overnight.log"

    if exit_file.is_file():
        exit_file.unlink()

    if pid_file.is_file():
        try:
            old_pid = int(pid_file.read_text().strip())
            if old_pid > 0 and Path(f"/proc/{old_pid}").exists():
                print(f"overnight runner already active pid={old_pid}", file=sys.stderr)
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
        "pid": os.getpid(),
        "overnight_run_id": None,
    }

    try:
        plan = load_overnight_import_plan(args.plan)
        result_payload["overnight_run_id"] = plan.overnight_run_id

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
                        f"resource env {key} must be {val} for overnight write"
                    )

        progress_root = args.progress_root
        runner = ControlledOvernightSequenceRunner(CATALOG_ROOT, progress_root=progress_root)
        seq_res = runner.run(
            plan,
            mode=args.mode,
            state_path=state_file,
            log_root=log_root,
        )
        result_payload["overnight_sequence"] = {
            "status": seq_res.status,
            "stopped_at_month": seq_res.stopped_at_month,
            "stopped_reason": seq_res.stopped_reason,
            "errors": seq_res.errors,
            "months": seq_res.months,
        }

        overall = {
            "overnight_run_id": plan.overnight_run_id,
            "backup_recovery_id": plan.backup_recovery_id,
            "pid": os.getpid(),
            "started_at": started,
            "mode": args.mode,
            "status": seq_res.status,
            "current_month": seq_res.stopped_at_month,
            "updated_at": _utc_iso(),
        }
        _atomic_write_json(progress_root / "overall_state.json", overall)

        if seq_res.status not in ("dry_run_ok", "readback_ok"):
            exit_code = 1
    except Exception as exc:  # noqa: BLE001
        result_payload["exception"] = repr(exc)
        exit_code = 1
    finally:
        result_payload["finished_at"] = _utc_iso()
        result_payload["wall_seconds"] = time.monotonic() - t0
        result_payload["exit_code"] = exit_code
        _atomic_write_json(state_file, result_payload)
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
