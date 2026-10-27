#!/usr/bin/env python3
"""BT-D82 — TP/SL lifecycle + SHORT_PAUSED shadow audit (research-only)."""

from __future__ import annotations

import copy
import csv
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "results/phase_bt_d82_tp_sl_pause_accounting_v1"
REPORT = ROOT / "reports/PHASE_BT_D82_TP_SL_PAUSE_ACCOUNTING_V1.md"
D81_BUNDLE = ROOT / "results/phase_bt_d81_seven_day_cache_replay_v1/cache_bundle"
D80_BUNDLE = ROOT / "results/phase_bt_d80_multichunk_cache_pilot_v1/cache_bundle"
D81_PKG = ROOT / "tools/phase_bt_d81_seven_day_cache_replay_v1"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"

_PATH_DEPS = (
    D81_PKG,
    ROOT / "tools/phase_bt_d73_selected_sep23_24_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ISOLATED,
)
for p in _PATH_DEPS:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from d81_window_v1 import EXPECTED_CLOSE_COUNT, WINDOW_END_UTC, WINDOW_START_UTC, iter_window_closes_inclusive  # noqa: E402
from exit_semantics_audit_v1 import (  # noqa: E402
    D46_PER_BAR_SOURCE,
    EXIT_CONTRACT_SOURCE,
    ISOLATED_RUNNER_GAP,
)
from known_case_regressions_v1 import run_known_case_regressions  # noqa: E402
from research_exit_ledger_v1 import build_research_ledger  # noqa: E402
from shadow_pause_audit_v1 import extract_shadow_candidates_during_pause  # noqa: E402


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _verify_d81_bundle() -> dict[str, Any]:
    from cache_store_v1 import load_bundle_manifest  # noqa: E402

    manifest_path = D81_BUNDLE / "bundle_manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "error": "D81 bundle manifest missing"}
    manifest = load_bundle_manifest(D81_BUNDLE)
    count = int(manifest.get("entry_count") or 0)
    return {
        "ok": count == EXPECTED_CLOSE_COUNT,
        "entry_count": count,
        "expected": EXPECTED_CLOSE_COUNT,
        "pipeline": manifest.get("required_pipeline_line_id"),
        "config_hash": manifest.get("required_config_hash"),
        "read_only": True,
    }


def _run_seven_day_executable_replay() -> dict[str, Any]:
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
    bar_keys, prices, ohlc_meta = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    gate = build_pure_ema_gate(bar_keys=bar_keys, prices=prices, isolated_strategy_config=isolated)
    pause = ShortPauseControllerV1()
    pause.set_ready_for_research_only()
    t0 = time.perf_counter()
    replay = run_isolated_chronological_replay(
        config=config,
        isolated_bundle=isolated,
        prices=prices,
        bars=closes,
        density_session=CacheOnlySession(create_density_session(config)),
        pause=pause,
        ema_gate=gate,
    )
    replay["_wall_s"] = round(time.perf_counter() - t0, 4)
    return {"config": config, "prices": prices, "replay": replay, "ohlc_meta": ohlc_meta, "closes": closes}


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _chart_review_events(replay: dict[str, Any], ledger: dict[str, Any], limit: int = 15) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for m in replay.get("modeled_positions") or []:
        events.append(
            {
                "decision_close_utc": str(m.get("decision_close_utc")),
                "chart_review_reason": f"EXECUTABLE_{m.get('event')}_v3",
                "pool_key": str(m.get("pool_key") or ""),
            }
        )
    for row in ledger.get("research_ledger_rows") or []:
        d46 = row.get("d46_research_exit") or {}
        if d46.get("exit_reason") in ("TP", "SL", "AMBIGUOUS_INTRABAR", "OPEN_AT_END"):
            events.append(
                {
                    "decision_close_utc": str(d46.get("exit_bar_utc") or ""),
                    "chart_review_reason": f"D46_RESEARCH_{d46.get('exit_reason')}",
                    "pool_key": str(row.get("pool_key") or ""),
                }
            )
    for s in (ledger.get("shadow_sample") or [])[:5]:
        events.append(
            {
                "decision_close_utc": str(s.get("decision_close_utc")),
                "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
                "pool_key": str(s.get("pool_key") or ""),
            }
        )
    return events[:limit]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    bundle_check = _verify_d81_bundle()
    (OUT / "d81_bundle_check.json").write_text(json.dumps(bundle_check, indent=2), encoding="utf-8")
    if not bundle_check.get("ok"):
        _write_report("TRADE_ACCOUNTING_BLOCKED", bundle_check, None, None, None)
        print("TRADE_ACCOUNTING_BLOCKED bundle", flush=True)
        return 2

    regressions = run_known_case_regressions(d81_bundle=D81_BUNDLE, d80_bundle=D80_BUNDLE)
    (OUT / "known_case_regressions.json").write_text(json.dumps(regressions, indent=2), encoding="utf-8")
    if not all(r.get("pass") for r in regressions):
        _write_report("TRADE_ACCOUNTING_BLOCKED", bundle_check, regressions, None, None)
        print("TRADE_ACCOUNTING_BLOCKED regressions", flush=True)
        return 3

    seven = _run_seven_day_executable_replay()
    replay = seven["replay"]
    ledger = build_research_ledger(
        config=seven["config"],
        replay=replay,
        prices=seven["prices"],
        horizon_end_utc=WINDOW_END_UTC,
    )
    shadow = extract_shadow_candidates_during_pause(replay)
    ledger["shadow_count"] = len(shadow)
    ledger["shadow_sample"] = shadow[:25]
    (OUT / "research_exit_ledger.json").write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    flat_rows = []
    for row in ledger["research_ledger_rows"]:
        d46 = row["d46_research_exit"]
        flat_rows.append(
            {
                "signal_close_utc": row["signal_close_utc"],
                "executable_fill_utc": row["executable_fill_utc"],
                "entry_price": row["entry_price"],
                "sl_price": row["sl_price"],
                "tp_price": row["tp_price"],
                "baseline_sl_exit_utc": row["baseline_sl_exit_utc"],
                "d46_exit_utc": d46.get("exit_bar_utc"),
                "d46_exit_reason": d46.get("exit_reason"),
                "d46_exit_price": d46.get("exit_price"),
                "gross_return_pct_short": d46.get("gross_return_pct_short"),
            }
        )
    _write_csv(
        OUT / "research_exit_ledger.csv",
        flat_rows,
        [
            "signal_close_utc",
            "executable_fill_utc",
            "entry_price",
            "sl_price",
            "tp_price",
            "baseline_sl_exit_utc",
            "d46_exit_utc",
            "d46_exit_reason",
            "d46_exit_price",
            "gross_return_pct_short",
        ],
    )
    _write_csv(
        OUT / "shadow_pause_candidates.csv",
        shadow,
        ["decision_close_utc", "pool_key", "ema_decision", "pause_state", "shadow_class", "executable"],
    )

    summary = {
        "verdict": "TRADE_ACCOUNTING_PASS",
        "replay_wall_s": replay.get("_wall_s"),
        "pool_candidates": len(replay.get("baseline_pool_candidates") or []),
        "authorized": len(replay.get("authorized_candidates") or []),
        "blocked": len(replay.get("blocked_candidates") or []),
        "executable_fills": ledger["fill_count_executable"],
        "shadow_pause_pool_ema_ok": len(shadow),
        "pause_state_final": replay.get("pause_state_final"),
        "intrabar_policy": seven["config"]["pnl"]["intrabar_ambiguity_policy"],
        "chart_review_events": _chart_review_events(replay, ledger, 15),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    _write_report("TRADE_ACCOUNTING_PASS", bundle_check, regressions, ledger, summary)
    print("TRADE_ACCOUNTING_PASS", flush=True)
    return 0


def _write_report(
    verdict: str,
    bundle_check: dict,
    regressions: list[dict] | None,
    ledger: dict | None,
    summary: dict | None,
) -> None:
    body = f"""# BT-D82 — TP/SL Lifecycle & SHORT_PAUSED Research Audit V1

**VERDICT:** `{verdict}`

**Baseline:** BT-D81 @ `f9d4a641` (tag `pre_bt_d82_stable_20261027`)  
**Git:** `{_git_head()}`  
**Window:** `{WINDOW_START_UTC}` … `{WINDOW_END_UTC}` (**{EXPECTED_CLOSE_COUNT}** closes, D81 cache read-only)

## B — Exit accounting audit

| Layer | TP exit | SL exit | Intrabar both-hit |
|-------|---------|---------|-------------------|
| **D46 / D7** (`simulate_short_exit`, `{D46_PER_BAR_SOURCE}`) | `low <= tp` | `high >= sl` | `pnl.intrabar_ambiguity_policy` |
| **Isolated research runner** (`isolated_chronological_runner_v1`) | **not modeled** | `high >= sl` only | not applied |
| **Gap** | {ISOLATED_RUNNER_GAP} |

**5m convention:** bar key = **decision close** UTC; OHLC is the completed bucket for that key.  
**Fill ordering (isolated):** per bar — SL check on open position → pending fill → EMA cancel → D41 pending review → new signals.  
**Chunk/day boundaries:** replay is one continuous state machine; density cache is per-close only (no strategy reset).

Research extension reuses **`{EXIT_CONTRACT_SOURCE}`** without modifying runners or BT-D46.

## D — Known case regressions

```json
{json.dumps(regressions, indent=2) if regressions else "null"}
```

- **Sep-20:** executable isolated SL @ **14:05** (fill @ 14:00). D46 `simulate_short_exit` records SL @ **14:00** because it scans the entry bar before isolated runner’s post-fill SL pass — documented ordering delta, not a policy gap.  
- **Sep-23 (3-day pilot window):** executable baseline leaves position **open** (no TP in runner); D46 research path records **TP @ 13:05** (hypothetical vs executable).

## E — SHORT_PAUSED shadow (not executable)

Shadow rows: **{(ledger or {}).get('shadow_count', 'n/a')}** (`shadow_pause_candidates.csv`).  
Criteria: `pause_state=SHORT_PAUSED`, pool present, `ema_decision=SHORT_OK`, `PAUSE_*` in gate reasons.

## F — Seven-day comparison (no density rebuild)

```json
{json.dumps(summary, indent=2) if summary else "null"}
```

**Executable chronology:** see `research_exit_ledger.json` / `.csv`.  
**Pause:** Sep-20 SL → `SHORT_PAUSED` for remainder; no auto-unlock.

### Chart-review timestamps (sample)

```json
{json.dumps((summary or {}).get('chart_review_events'), indent=2) if summary else "[]"}
```

## G — Before 30-day / profitability

- Executable PnL still **SL-only** in isolated runner; use **D46 research ledger** for TP/OPEN_AT_END semantics.  
- No fees, slippage, or leverage configured.  
- Shadow signals are **not** trades.  
- Intrabar ordering when **both** SL and TP touched: **`no_trade_ambiguous`** → no price exit.

## Appendix — D81 bundle check

```json
{json.dumps(bundle_check, indent=2)}
```

**STOP.**
"""
    REPORT.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
