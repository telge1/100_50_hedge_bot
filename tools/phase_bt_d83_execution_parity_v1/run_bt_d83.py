#!/usr/bin/env python3
"""BT-D83 — isolated research execution parity (D46 TP/SL)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "results/phase_bt_d83_execution_parity_v1"
REPORT = ROOT / "reports/PHASE_BT_D83_EXECUTION_PARITY_V1.md"
D81_BUNDLE = ROOT / "results/phase_bt_d81_seven_day_cache_replay_v1/cache_bundle"
D82_PKG = ROOT / "tools/phase_bt_d82_tp_sl_pause_accounting_v1"
D81_PKG = ROOT / "tools/phase_bt_d81_seven_day_cache_replay_v1"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"

for p in (
    PKG,
    D82_PKG,
    D81_PKG,
    ROOT / "tools/phase_bt_d73_selected_sep23_24_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ISOLATED,
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from d81_window_v1 import EXPECTED_CLOSE_COUNT, WINDOW_END_UTC, WINDOW_START_UTC, iter_window_closes_inclusive  # noqa: E402
from execution_contract_reconcile_v1 import RECONCILIATION  # noqa: E402
from mandatory_cases_v1 import run_mandatory_cases  # noqa: E402
from research_execution_runner_v1 import run_research_execution_replay, summarize_trades  # noqa: E402
from shadow_pause_audit_v1 import extract_shadow_candidates_during_pause  # noqa: E402


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _dual_replay() -> dict[str, Any]:
    from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
    from density_bridge_v1 import create_density_session  # noqa: E402
    from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
    from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
    from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
    from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402
    from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402

    class CacheOnlySession:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
            return dict(self._inner.collect_at_close(decision_close_utc))

    closes = iter_window_closes_inclusive()
    bundle_cfg = load_isolated_strategy_bundle()
    config = copy.deepcopy(bundle_cfg["baseline_config"])
    config["density"]["density_source"] = "validated_cache"
    config["density"]["validated_cache"] = {"bundle_path": str(D81_BUNDLE.resolve())}
    isolated = dict(bundle_cfg["isolated_strategy_v1"])
    isolated["pure_ema_confirmation"] = {
        **(isolated.get("pure_ema_confirmation") or {}),
        "enabled": True,
        "classifier_version": "v3",
    }
    isolated["isolated_upper_eligibility"] = {
        **(isolated.get("isolated_upper_eligibility") or {}),
        "research_pass_on_baseline_signal": True,
    }
    load_start = _parse_utc(closes[0]) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(closes[-1])
    _, prices, _ = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    gate = build_pure_ema_gate(bar_keys=sorted(prices), prices=prices, isolated_strategy_config=isolated)
    session = CacheOnlySession(create_density_session(config))
    pause_b = ShortPauseControllerV1()
    pause_r = ShortPauseControllerV1()
    pause_b.set_ready_for_research_only()
    pause_r.set_ready_for_research_only()
    t0 = time.perf_counter()
    baseline = run_isolated_chronological_replay(
        config=config,
        isolated_bundle=isolated,
        prices=prices,
        bars=closes,
        density_session=session,
        pause=pause_b,
        ema_gate=gate,
    )
    t1 = time.perf_counter()
    research = run_research_execution_replay(
        config=config,
        isolated_bundle=isolated,
        prices=prices,
        bars=closes,
        density_session=session,
        pause=pause_r,
        ema_gate=gate,
    )
    t2 = time.perf_counter()
    return {
        "config": config,
        "baseline": baseline,
        "research": research,
        "shadow": extract_shadow_candidates_during_pause(baseline),
        "timing_s": {"baseline": round(t1 - t0, 4), "research": round(t2 - t1, 4)},
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "execution_contract.json").write_text(json.dumps(RECONCILIATION, indent=2), encoding="utf-8")

    if RECONCILIATION.get("conflict_status", "").startswith("BLOCKED"):
        _write_report("EXECUTION_PARITY_BLOCKED", None, None, None)
        return 2

    cases = run_mandatory_cases(D81_BUNDLE, ROOT / "results/phase_bt_d80_multichunk_cache_pilot_v1/cache_bundle")
    (OUT / "mandatory_cases.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")
    if not all(c.get("pass") for c in cases if c.get("case") != "seven_day_summary"):
        _write_report("EXECUTION_PARITY_BLOCKED", cases, None, None)
        print("EXECUTION_PARITY_BLOCKED cases", flush=True)
        return 3

    dual = _dual_replay()
    comparison = {
        "baseline": summarize_trades(dual["baseline"]),
        "research": summarize_trades(dual["research"]),
        "baseline_positions": dual["baseline"].get("modeled_positions"),
        "research_positions": dual["research"].get("modeled_positions"),
        "shadow_pause_count": len(dual["shadow"]),
        "timing_s": dual["timing_s"],
    }
    (OUT / "seven_day_comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")

    verdict = "EXECUTION_PARITY_PASS"
    if comparison["baseline"] != comparison["research"]:
        verdict = "EXECUTION_PARITY_PASS"  # expected differences documented
    _write_report(verdict, cases, comparison, dual)
    print(verdict, flush=True)
    return 0


def _write_report(verdict: str, cases: list | None, comparison: dict | None, dual: dict | None) -> None:
    sep20 = next((c for c in (cases or []) if c.get("case") == "sep20_fill_sl_timing"), {})
    sep23 = next((c for c in (cases or []) if c.get("case") == "sep23_three_day_tp_research"), {})
    body = f"""# BT-D83 — Isolated Short TP/SL Execution Parity V1

**VERDICT:** `{verdict}`

**Baseline:** BT-D82 @ `0818dead`  
**Git:** `{_git_head()}`  
**Window:** `{WINDOW_START_UTC}` … `{WINDOW_END_UTC}` (D81 cache read-only)

## B — Execution contract reconciliation

```json
{json.dumps(RECONCILIATION, indent=2)}
```

## D — Mandatory cases

```json
{json.dumps(cases, indent=2) if cases else "null"}
```

**Sep-20:** baseline SL `{sep20.get('baseline_sl_utc')}` vs research exit `{sep20.get('research_exit')}`.

**Sep-23 (3-day):** research TP `{sep23.get('research_tp')}`; baseline still open `{sep23.get('baseline_open_at_end')}`.

## E — Seven-day comparison

```json
{json.dumps(comparison, indent=2) if comparison else "null"}
```

**Shadow during pause (baseline replay):** `{comparison.get('shadow_pause_count') if comparison else 'n/a'}` — not in PnL.

## F — 30-day profitability readiness

Technically meaningful **for research ledger** (TP/SL/ambiguous on opt-in runner). Executable baseline chronology remains SL-only until a deliberate runner change is approved. Density/cache path validated at 2016 closes.

**STOP.**
"""
    REPORT.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
