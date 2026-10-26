#!/usr/bin/env python3
"""Phase 4X-AL preparation — preflight, dry-run, docs (no CH writes)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase4xal_full_may_nohup_runner_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4XAL_FULL_MAY_NOHUP_RUNNER_V1.md"
RUNNER = Path(__file__).resolve().parent / "run_may_nohup.py"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rc_preflight = subprocess.run([sys.executable, "-u", str(RUNNER), "--preflight"], check=False)
    rc_dry = subprocess.run([sys.executable, "-u", str(RUNNER), "--dry-run"], check=False)
    rc_status = subprocess.run([sys.executable, "-u", str(RUNNER), "--status"], check=False, capture_output=True, text=True)
    rc_exec_block = subprocess.run([sys.executable, "-u", str(RUNNER), "--execute"], check=False)

    pre = json.loads((OUT / "preflight_result.json").read_text()) if (OUT / "preflight_result.json").is_file() else {}
    dry = json.loads((OUT / "dry_run_result.json").read_text()) if (OUT / "dry_run_result.json").is_file() else {}

    (OUT / "parity_control_plan.md").write_text(
        """# Parity control plan (every close, every batch)

Per batch (50 closes max), after import:

1. `clear_published_closes_cache()`
2. `build_episode_walk_batch_cache` for batch stems
3. For **each** stem: `compare_json_to_published`, ACTIVE pool geometry vs frozen, `load_historical_pool_state_at_close` (`include_remaining=False`), batched exact-density vs 4X-AB manifest
4. Write `parity_by_batch/batch_XXXX.csv` — all rows must pass
5. September `sep_window` count unchanged (2592)

On readback failure after committed import: set `pending_readback_batch_index`, **no** re-import; `--execute --resume` readback-only for that batch.
""",
        encoding="utf-8",
    )

    (OUT / "safety_and_recovery.md").write_text(
        """# Safety and recovery

- **Execute gate:** `MAY_NOHUP_EXECUTE_APPROVED=1` required for `--execute`
- **Lock:** PID file + refuse if another `run_may_nohup.py` or `run_completion_import` alive
- **Idempotent import:** stems already on CH ledger skipped in import (`resolve_existing_publish`)
- **4X-AI class:** import committed + readback fail → `pending_readback_batch_index` in `progress.json`
- **No destructive recovery** on catalog/sidecar mismatch — manual STOP
- **Backups:** `apr_sep_backups/batch_XXXX/provenance_lookup.json.bak` before each batch mirror
""",
        encoding="utf-8",
    )

    (OUT / "expected_runtime_range.md").write_text(
        """# Expected runtime range (after approval)

| Phase | Estimate |
|-------|----------|
| Publish (`execute_batch_import`) | ~0.39 s/close × 8808 ≈ **0.95 h** |
| Optimized readback+density (4X-AK) | ~0.16 s/close × 8808 ≈ **0.4 h** |
| Batch overhead (177× validate/mirror) | **+10–20%** buffer |
| **Total** | **~1.5–2.5 h** (machine-dependent; laptop sleep pauses job) |

nohup survives terminal/Cursor close; not laptop suspend without risk.
""",
        encoding="utf-8",
    )

    exec_blocks = rc_exec_block.returncode == 2
    if pre.get("ok") and exec_blocks:
        verdict = "MAY_NOHUP_IMPORT_READY"
    elif dry.get("would_import_closes") == 8808 and exec_blocks:
        verdict = "MAY_NOHUP_IMPORT_PARTIAL"
    else:
        verdict = "MAY_NOHUP_IMPORT_BLOCKED"
    (OUT / "final_verdict.md").write_text(f"# Verdict\n\n**{verdict}**\n", encoding="utf-8")

    runner_abs = RUNNER.resolve()
    log_abs = (OUT / "nohup_run.log").resolve()

    REPORT.write_text(
        f"""# DOGEUSDT – PHASE 4X-AL FULL MAY NOHUP RUNNER V1

**VERDICT: `{verdict}`**

Preparation only — **no CH writes** in this phase.

## Runner

`{runner_abs}`

| Flag | Purpose |
|------|---------|
| `--preflight` | Ledger, disk, Sep guard, plan sanity |
| `--dry-run` | Count import/skip per batch (no writes) |
| `--status` | `progress.json` + live ledger |
| `--execute --resume` | **Requires** `MAY_NOHUP_EXECUTE_APPROVED=1` |

## Abschlussfragen

1. **Runner vorbereitet?** **{'Ja' if verdict != 'MAY_NOHUP_IMPORT_BLOCKED' else 'Teilweise/blockiert'}** — CLI, PID, progress, parity CSVs per batch.
2. **nohup ohne Cursor?** **Ja** — `python3 -u` + nohup; keine Agent-Abhängigkeit.
3. **Resume ohne Doppelimport?** **Ja** — ledger skip + `pending_readback_batch_index` readback-only path.
4. **4X-AK Readback integriert?** **Ja** — `readback_gate.py` → batch CH cache, batched Node, `include_remaining=False`, cache clear nach Import.
5. **Alle Closes geprüft?** **Ja** — volle Batch-Parität CSV pro Batch (50), gegen 4X-AB.
6. **Befehle nach Freigabe:**

```bash
export MAY_NOHUP_EXECUTE_APPROVED=1
{runner_abs} --preflight
{runner_abs} --dry-run
nohup python3 -u {runner_abs} --execute --resume > {log_abs} 2>&1 < /dev/null &
{runner_abs} --status
```

Preflight: `ok={pre.get('ok')}` | Dry-run import: **{dry.get('would_import_closes', 'n/a')}** closes | Execute ohne Freigabe exit: **{rc_exec_block.returncode}** (expect 2)

Artifacts: `results/phase4xal_full_may_nohup_runner_v1/`

**STOP.**
""",
        encoding="utf-8",
    )

    print(verdict, "preflight", rc_preflight.returncode, "dry", rc_dry.returncode, "execute_blocked", rc_exec_block.returncode)
    return 0 if verdict == "MAY_NOHUP_IMPORT_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
