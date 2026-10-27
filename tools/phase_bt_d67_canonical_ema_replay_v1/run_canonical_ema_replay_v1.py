#!/usr/bin/env python3
"""BT-D67 — canonical density + EMA-gated isolated chronological replay."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "results/phase_bt_d67_canonical_ema_replay_v1"
REPORT = ROOT / "reports/PHASE_BT_D67_CANONICAL_EMA_SHORT_REPLAY_V1.md"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"
D60_MANIFEST = ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json"

for p in (
    PKG,
    ISOLATED,
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from pilot_density_session_v1 import build_pilot_density_session, preflight_coverage  # noqa: E402
from pilot_windows_v1 import PILOT_WINDOWS, REFERENCE_CASES  # noqa: E402
from pure_ema_confirmation_v1 import PureEmaConfirmationGateV1  # noqa: E402
from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1  # noqa: E402
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402


def _bars_between(bar_keys: list[str], start: str, end: str) -> list[str]:
    return [b for b in bar_keys if start <= b <= end]


def _write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = fields or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _run_pilot(
    *,
    pilot: dict,
    config: dict,
    isolated: dict,
    bar_keys: list[str],
    prices: dict,
    ema_gate: PureEmaConfirmationGateV1,
    pause_ready: bool,
) -> dict:
    session = build_pilot_density_session(config, pilot)
    bars = _bars_between(bar_keys, pilot["start_utc"], pilot["end_utc"])
    coverage = preflight_coverage(session, bars)
    incomplete = [r for r in coverage if r["coverage_status"] != "OK"]
    pause = ShortPauseControllerV1()
    if pause_ready:
        pause.set_ready_for_research_only()
    replay = run_isolated_chronological_replay(
        config=config,
        isolated_bundle=isolated,
        prices=prices,
        bars=bars,
        density_session=session,
        pause=pause,
        ema_gate=ema_gate,
    )
    replay["pilot_id"] = pilot["pilot_id"]
    replay["pause_mode"] = "RESEARCH_READY" if pause_ready else "DEFAULT_SHORT_PAUSED"
    replay["coverage_preflight"] = coverage
    replay["coverage_incomplete_count"] = len(incomplete)
    return replay


def main() -> int:
    if not D60_MANIFEST.is_file():
        raise SystemExit("BT-D60 manifest missing")
    d60 = json.loads(D60_MANIFEST.read_text())
    if d60.get("verdict") != "EMA_HISTORY_VALIDATED":
        raise SystemExit("BT-D60 not validated")

    bundle = load_isolated_strategy_bundle()
    isolated = dict(bundle["isolated_strategy_v1"])
    isolated["pure_ema_confirmation"] = {**(isolated.get("pure_ema_confirmation") or {}), "enabled": True}
    isolated["isolated_upper_eligibility"] = {
        **(isolated.get("isolated_upper_eligibility") or {}),
        "research_pass_on_baseline_signal": True,
    }
    config = bundle["baseline_config"]

    g_start = min(p["start_utc"] for p in PILOT_WINDOWS)
    g_end = max(p["end_utc"] for p in PILOT_WINDOWS)
    load_start = _parse_utc(g_start) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    bar_keys, prices, ohlc_meta = load_5m_ohlc_ch(_iso(load_start), g_end)
    ema_provider = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices)
    ema_gate = PureEmaConfirmationGateV1(provider=ema_provider)

    ref_rows = []
    for ts, expected in REFERENCE_CASES:
        conf = ema_gate.at_close(ts)
        ref_rows.append(
            {
                "decision_close_utc": ts,
                "expected_ema": expected,
                "actual_ema": conf.decision,
                "match": conf.decision == expected,
                "reason_code": conf.reason_code,
            }
        )

    default_runs = [_run_pilot(pilot=p, config=config, isolated=isolated, bar_keys=bar_keys, prices=prices, ema_gate=ema_gate, pause_ready=False) for p in PILOT_WINDOWS]
    research_runs = [_run_pilot(pilot=p, config=config, isolated=isolated, bar_keys=bar_keys, prices=prices, ema_gate=ema_gate, pause_ready=True) for p in PILOT_WINDOWS]

    all_coverage: list[dict] = []
    all_baseline: list[dict] = []
    all_ema_cand: list[dict] = []
    all_lifecycle: list[dict] = []
    all_positions: list[dict] = []
    all_blocked: list[dict] = []
    all_gate: list[dict] = []
    comparison: list[dict] = []

    for dr, rr in zip(default_runs, research_runs):
        pid = dr["pilot_id"]
        for row in dr.get("coverage_preflight") or []:
            all_coverage.append({**row, "pilot_id": pid})
        for row in dr.get("baseline_pool_candidates") or []:
            all_baseline.append({**row, "pilot_id": pid})
        for row in dr.get("ema_shadow_at_candidates") or []:
            all_ema_cand.append({**row, "pilot_id": pid, "track": "shadow_default_pause"})
        for row in rr.get("ema_shadow_at_candidates") or []:
            all_ema_cand.append({**row, "pilot_id": pid, "track": "shadow_research_ready"})
        for row in rr.get("pending_lifecycle") or []:
            all_lifecycle.append({**row, "pilot_id": pid, "pause_mode": rr["pause_mode"]})
        for row in rr.get("modeled_positions") or []:
            all_positions.append({**row, "pilot_id": pid})
        for row in rr.get("blocked_candidates") or []:
            all_blocked.append({**row, "pilot_id": pid})
        for row in rr.get("gate_decisions") or []:
            all_gate.append({**row, "pilot_id": pid, "pause_mode": rr["pause_mode"]})
        comparison.append(
            {
                "pilot_id": pid,
                "bars_processed_default": dr["bars_processed"],
                "bars_processed_research": rr["bars_processed"],
                "pool_candidates_default": dr["pool_candidates_seen"],
                "pool_candidates_research": rr["pool_candidates_seen"],
                "pending_created_default": dr["pending_orders_created"],
                "pending_created_research": rr["pending_orders_created"],
                "modeled_fills_research": rr["modeled_fills"],
                "coverage_incomplete_preflight": dr["coverage_incomplete_count"],
            }
        )

    timing = []
    for rr in research_runs:
        for n in rr.get("timing_notes") or []:
            timing.append({**n, "pilot_id": rr["pilot_id"]})

    pause_audit = [
        {
            "pilot_id": r["pilot_id"],
            "pause_mode": r["pause_mode"],
            "pending_orders_created": r["pending_orders_created"],
            "pool_candidates_seen": r["pool_candidates_seen"],
            "pause_state_final": r["pause_state_final"],
        }
        for r in default_runs + research_runs
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    provenance = {
        "phase": "BT-D67",
        "git_head": _git_head(),
        "bt_d60_manifest_sha256": _sha(D60_MANIFEST),
        "density_bridge": "tools/phase_bt_d7_config_driven_short_pnl_integration_v1/density_bridge_v1.py",
        "ohlc_meta": ohlc_meta,
        "pilot_windows": PILOT_WINDOWS,
        "warmup_hours": REFERENCE_WARMUP_HOURS,
    }
    (OUT / "input_provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    _write_csv(OUT / "ema_gate_decisions.csv", all_gate)
    _write_csv(OUT / "coverage_audit.csv", all_coverage)
    _write_csv(OUT / "baseline_pool_candidates.csv", all_baseline)
    _write_csv(OUT / "ema_gate_at_candidates.csv", all_ema_cand)
    _write_csv(OUT / "pending_lifecycle.csv", all_lifecycle)
    _write_csv(OUT / "modeled_positions.csv", all_positions)
    _write_csv(OUT / "baseline_vs_ema_comparison.csv", comparison)
    _write_csv(OUT / "reference_case_audit.csv", ref_rows)
    _write_csv(OUT / "pause_state_audit.csv", pause_audit)
    _write_csv(OUT / "unresolved_timing_cases.csv", timing)

    ref_ok = all(r["match"] for r in ref_rows)
    cov_ok = all(c["coverage_status"] == "OK" for c in all_coverage)
    verdict = "CANONICAL_EMA_REPLAY_PASS" if ref_ok and cov_ok else "CANONICAL_EMA_REPLAY_PARTIAL"
    if any(c["coverage_status"] == "INCOMPLETE" for c in all_coverage):
        if not any(c["coverage_status"] == "OK" for c in all_coverage):
            verdict = "CANONICAL_EMA_REPLAY_BLOCKED"

    REPORT.write_text(
        "\n".join(
            [
                "# BT-D67 — Canonical EMA-Gated Short Replay V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                f"Git HEAD: `{provenance['git_head']}`",
                "",
                "## Pilots",
                "- `sep9_evening_2026` 16:00–22:00 UTC (D54 snapshots + canonical gaps)",
                "- `sep11_midday_2026` 11:30–13:30 UTC (D9 cache + canonical gaps)",
                "",
                "## Reference EMA (BT-D65, pool-independent)",
                f"- Mandatory cases match: **{ref_ok}**",
                "- Note: `2026-09-09T16:30` labels `UNCERTAIN` (`FALLING_EMAS_PRICE_RECLAIM`) on current TRP OHLC — see `reference_case_audit.csv` (not R5/R6).",
                "",
                "## Coverage",
                f"- Preflight complete bars: **{sum(1 for c in all_coverage if c['coverage_status']=='OK')}** / **{len(all_coverage)}**",
                "",
                "## Pause separation",
                "- `DEFAULT_SHORT_PAUSED`: fail-closed (no pending unless explicitly unpaused).",
                "- `RESEARCH_READY`: hypothetical comparator for chronological pending/fill under EMA gate.",
                "",
                "Artifacts: `results/phase_bt_d67_canonical_ema_replay_v1/`",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "ref_ok": ref_ok, "coverage_ok": cov_ok}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
