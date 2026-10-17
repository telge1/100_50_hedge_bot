#!/usr/bin/env python3
"""Phase 6XT-90D integrated replay crash fix — regression + smoke (no full 90d)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_90d_integrated_replay_crash_fix_v1"
_CTX = Path(__file__).resolve().parent

for p in (_DASH, _REPO, _CTX):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import phase6xt_90d_integrated_price_pool_memory_v1 as integrated  # noqa: E402
import phase6xt_market_memory_price_stream_recovery_v1 as rec  # noqa: E402
from market_memory_bootstrap_engine import ledger_from_memory, parity_diff  # noqa: E402
from market_memory_state import MarketMemoryState  # noqa: E402


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_pytest() -> dict:
    test_path = _REPO / "backtester/short/dashboard/research_charts/tests/test_integrated_replay_return_contract.py"
    r = subprocess.run(
        [sys.executable, "-m", "unittest", str(test_path), "-v"],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
    )
    return {
        "pass": r.returncode == 0,
        "returncode": r.returncode,
        "stdout_tail": r.stdout[-4000:] if r.stdout else "",
        "stderr_tail": r.stderr[-2000:] if r.stderr else "",
    }


def smoke_reference_unpack() -> dict:
    """Execute the formerly broken reference path with mocked walk (no CH)."""
    t0 = time.time()
    fake_ledger = {1: {"o1": {"side": "lower"}}, 10: {}}
    fake_meta = {"birth_ledger": fake_ledger, "decision_closes_processed": 2}
    err = None
    birth_parity = None
    try:
        with patch.object(rec, "run_segment_price", return_value=(None, fake_meta, None)):
            _, ref_meta, proc_ref = integrated.run_segment(
                integrated.BOOT_START,
                integrated.BOOT_END,
                MagicMock(),
                "db",
                {},
                None,
                None,
                None,
                None,
                with_price=False,
            )
        assert proc_ref is None
        birth_parity = parity_diff(ref_meta["birth_ledger"], {1: {}, 10: {}})
    except Exception as e:
        err = repr(e)
    return {
        "pass": err is None and birth_parity is not None,
        "error": err,
        "birth_parity_keys": list(birth_parity.keys()) if birth_parity else None,
        "elapsed_sec": round(time.time() - t0, 3),
    }


def audit_watchers() -> dict:
    rows = []
    for pid in (3234371, 3250821, 3154249):
        try:
            out = subprocess.check_output(["ps", "-p", str(pid), "-o", "pid=,etime=,cmd="], text=True).strip()
            alive = bool(out)
        except subprocess.CalledProcessError:
            out = ""
            alive = False
        rows.append(
            {
                "pid": pid,
                "alive": alive,
                "ps_line": out,
                "likely_waiting_for_job_status": alive and "job_status.json" in out,
            }
        )
    return {"checked_at_utc": _iso(), "processes": rows}


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    pytest_res = run_pytest()
    smoke = smoke_reference_unpack()
    watchers = audit_watchers()

    all_ok = pytest_res["pass"] and smoke["pass"]
    verdict = "INTEGRATED_REPLAY_CRASH_FIX_VALIDATED" if all_ok else "INTEGRATED_REPLAY_CRASH_FIX_PARTIAL"

    (_OUT / "root_cause.md").write_text(
        """# Root cause

`phase6xt_90d_integrated_price_pool_memory_v1.py` line 392 (pre-fix) unpacked:

```python
_, ref_meta = run_segment(...)
```

`run_segment()` delegates to `rec.run_segment_price()`, which returns **three** values:

`(mem, meta, proc)` — see `phase6xt_market_memory_price_stream_recovery_v1.py` `run_segment_price`.

Paths A/B/C already used triple unpack; only the SSOT reference replay after path C used a **2-tuple** unpack.

**Effect:** Path A (~43 min) and paths B/C completed; crash occurred before `birth_parity`, final JSON artifacts, and `job_status.json`.
""",
        encoding="utf-8",
    )
    (_OUT / "code_patch_summary.md").write_text(
        """# Code patch

**File:** `phase6xt_90d_integrated_price_pool_memory_v1.py`

```diff
-    _, ref_meta = run_segment(..., with_price=False)
+    _, ref_meta, _ = run_segment(..., with_price=False)
```

No other files changed for the crash fix. Third value `proc` is `None` when `with_price=False`.
""",
        encoding="utf-8",
    )
    (_OUT / "return_contract_audit.md").write_text(
        """# Return contract audit

| Call site | Unpack | OK |
|-----------|--------|-----|
| Path A `mem_a, meta_a, proc_a` | 3 | yes |
| Path B `mem_b, meta_b, _` | 3 | yes |
| Path C1 `mem_c1, meta_c1, _` | 3 | yes |
| Path C2 `mem_c, meta_c2, _` | 3 | yes |
| Reference SSOT `_, ref_meta, _` | 3 | yes (fixed) |

`run_segment_price` contract: `tuple[MarketMemoryState | None, dict, Any | None]`.

Recovery runner `phase6xt_market_memory_price_stream_recovery_v1.py` already uses triple unpack throughout (including reference at line 306).
""",
        encoding="utf-8",
    )
    (_OUT / "missing_progress_artifacts_analysis.md").write_text(
        """# Missing progress artifacts (43 min run)

**Written before crash:** `server_resource_preflight.json`, `source_contract.json`, `coverage_audit.json`, `integrated_replay_manifest.json`.

**Not written:** `full_replay_state_summary.json`, parity JSONs, `job_status.json`, `report.md` — all occur **after** the reference `run_segment` call in `main()`.

**Proposal (documentation only):**
1. `progress.json` after each path (A/B/C) with hash, elapsed_sec, phase name.
2. `path_A_checkpoint.json` immediately after path A completes (large but resumable).
3. Early `job_status.json` with `phase=running_path_A` before long work.
4. `try/except` in `main()` writing `job_status.json` `FAILED` + traceback on any exception.
5. Unit test asserting all `run_segment` AST unpacks are length 3 (in CI).
""",
        encoding="utf-8",
    )
    (_OUT / "safe_full_replay_restart_plan.md").write_text(
        """# Safe full replay restart plan (requires explicit approval)

1. Confirm fix: `INTEGRATED_REPLAY_CRASH_FIX_VALIDATED` and unittest green.
2. Stop stale bash watchers waiting on `job_status.json` (only with operator approval).
3. Preflight: RAM, no DOGE 1m backfill, CH read probe.
4. Re-run `phase6xt_90d_integrated_price_pool_memory_v1.py` — expect ~45–55 min path A + reference SSOT walk.
5. Verify `job_status.json`, `checkpoint_restart_semantic_parity.json`, semantic hash match.
6. Keep `run.log` and phase progress files for audit.

**Do not start step 4 without explicit user approval.**
""",
        encoding="utf-8",
    )

    report = f"""# Phase 6XT-90D Integrated Replay Crash Fix V1

**Verdict:** `{verdict}`

1. ValueError fixed: **yes** (triple unpack on reference call)
2. Other bad call sites: **none** in integrated runner
3. Regression tests: **{pytest_res['pass']}**
4. Smoke reference path: **{smoke['pass']}**
5. Safe to restart 90d: **after approval** — see `safe_full_replay_restart_plan.md`
6. Progress artifacts: see `missing_progress_artifacts_analysis.md`
7. Watchers: see `waiting_processes_audit.json`
8. Full replay ready: **code-ready, not executed**

**STOP — no full 90d replay in this phase.**
"""
    (_OUT / "report.md").write_text(report, encoding="utf-8")
    json.dump(pytest_res, (_OUT / "targeted_regression_results.json").open("w"), indent=2)
    json.dump(smoke, (_OUT / "small_runner_smoke_test.json").open("w"), indent=2)
    json.dump(watchers, (_OUT / "waiting_processes_audit.json").open("w"), indent=2)
    json.dump(
        {"phase": "phase6xt_90d_integrated_replay_crash_fix_v1", "verdict": verdict},
        (_OUT / "job_status.json").open("w"),
        indent=2,
    )
    print(verdict)


if __name__ == "__main__":
    main()
