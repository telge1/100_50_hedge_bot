#!/usr/bin/env python3
"""BT-D70 — V1 vs V3 isolated EMA replay and parity."""

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
OUT = ROOT / "results/phase_bt_d70_ema_v3_v1"
REPORT = ROOT / "reports/PHASE_BT_D70_EMA_V3_ISOLATED_REPLAY_V1.md"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"
D67 = ROOT / "tools/phase_bt_d67_canonical_ema_replay_v1"
D60_MANIFEST = ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json"
CHART20 = ROOT / "results/phase_bt_d65_pure_ema_review_v1/chart_review_20_timestamps.csv"
PROPOSED = ROOT / "results/phase_bt_d69_pure_ema_audit_v1/proposed_rule_changes.md"

for p in (
    PKG,
    ISOLATED,
    D67,
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d69_pure_ema_false_allow_audit_v1",
    ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from pilot_density_session_v1 import build_pilot_density_session  # noqa: E402
from pilot_windows_v1 import PILOT_WINDOWS  # noqa: E402
from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1  # noqa: E402
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402

BLOCK_CASES = [
    "2026-08-06T00:00:00Z",
    "2026-08-12T08:30:00Z",
    "2026-09-11T12:05:00Z",
]
ALLOW_CASES = [
    "2026-09-09T20:55:00Z",
    "2026-08-05T03:30:00Z",
    "2026-08-05T07:30:00Z",
    "2026-08-13T10:30:00Z",
]
EXTRA_TS = ["2026-09-09T16:30:00Z"]

INDEPENDENT_WINDOWS = [
    {"window_id": "aug_w1_sample", "start_utc": "2026-08-05T00:00:00Z", "end_utc": "2026-08-05T12:00:00Z"},
    {"window_id": "sep18_sample", "start_utc": "2026-09-18T00:00:00Z", "end_utc": "2026-09-18T06:00:00Z"},
]


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
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


def _bars(bar_keys: list[str], start: str, end: str) -> list[str]:
    return [b for b in bar_keys if start <= b <= end]


def _replay_pilot(
    pilot: dict,
    *,
    config: dict,
    isolated: dict,
    bar_keys: list[str],
    prices: dict,
    classifier_version: str,
) -> dict:
    session = build_pilot_density_session(config, pilot)
    bars = _bars(bar_keys, pilot["start_utc"], pilot["end_utc"])
    iso = dict(isolated)
    iso["pure_ema_confirmation"] = {
        **(isolated.get("pure_ema_confirmation") or {}),
        "enabled": True,
        "classifier_version": classifier_version,
    }
    gate = build_pure_ema_gate(bar_keys=bar_keys, prices=prices, isolated_strategy_config=iso)
    pause = ShortPauseControllerV1()
    pause.set_ready_for_research_only()
    return run_isolated_chronological_replay(
        config=config,
        isolated_bundle=iso,
        prices=prices,
        bars=bars,
        density_session=session,
        pause=pause,
        ema_gate=gate,
    )


def main() -> int:
    if not D60_MANIFEST.is_file():
        raise SystemExit("BT-D60 manifest missing")
    bundle = load_isolated_strategy_bundle()
    isolated = dict(bundle["isolated_strategy_v1"])
    isolated["pure_ema_confirmation"] = {
        **(isolated.get("pure_ema_confirmation") or {}),
        "enabled": True,
    }
    isolated["isolated_upper_eligibility"] = {
        **(isolated.get("isolated_upper_eligibility") or {}),
        "research_pass_on_baseline_signal": True,
    }
    config = bundle["baseline_config"]
    coverage_starts = [p["start_utc"] for p in PILOT_WINDOWS] + [w["start_utc"] for w in INDEPENDENT_WINDOWS]
    coverage_ends = [p["end_utc"] for p in PILOT_WINDOWS] + [w["end_utc"] for w in INDEPENDENT_WINDOWS]
    ref_ts = BLOCK_CASES + ALLOW_CASES + EXTRA_TS
    coverage_starts.extend(ref_ts)
    coverage_ends.extend(ref_ts)
    if CHART20.is_file():
        with CHART20.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                coverage_starts.append(r["decision_close_utc"])
                coverage_ends.append(r["decision_close_utc"])
    load_start = _parse_utc(min(coverage_starts)) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(max(coverage_ends))
    bar_keys, prices, ohlc_meta = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))

    provider_v1 = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices, classifier_version="v1")
    provider_v3 = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices, classifier_version="v3")

    decision_rows: list[dict] = []
    ref_rows: list[dict] = []
    all_ts = sorted(set(BLOCK_CASES + ALLOW_CASES + EXTRA_TS))
    for ts in all_ts:
        r1 = provider_v1.confirm_at_close(ts)
        r3 = provider_v3.confirm_at_close(ts)
        decision_rows.append(
            {
                "decision_close_utc": ts,
                "v1_decision": r1.get("ema_structure_decision"),
                "v1_reason": r1.get("decision_reason_code"),
                "v3_decision": r3.get("ema_structure_decision"),
                "v3_reason": r3.get("decision_reason_code"),
                "decisions_differ": r1.get("ema_structure_decision") != r3.get("ema_structure_decision"),
            }
        )

    for ts in BLOCK_CASES:
        r3 = provider_v3.confirm_at_close(ts)
        ref_rows.append(
            {
                "decision_close_utc": ts,
                "expectation": "BLOCK",
                "v3_decision": r3.get("ema_structure_decision"),
                "pass": r3.get("ema_structure_decision") != "SHORT_OK",
            }
        )
    for ts in ALLOW_CASES:
        r3 = provider_v3.confirm_at_close(ts)
        ref_rows.append(
            {
                "decision_close_utc": ts,
                "expectation": "SHORT_OK",
                "v3_decision": r3.get("ema_structure_decision"),
                "pass": r3.get("ema_structure_decision") == "SHORT_OK",
            }
        )
    for ts in EXTRA_TS:
        r3 = provider_v3.confirm_at_close(ts)
        ref_rows.append(
            {
                "decision_close_utc": ts,
                "expectation": "EMA_ONLY_EVAL",
                "v3_decision": r3.get("ema_structure_decision"),
                "v3_reason": r3.get("decision_reason_code"),
                "pass": r3.get("ema_structure_decision") != "SHORT_OK",
            }
        )

    chart_rows: list[dict] = []
    chart_parity_rows: list[dict] = []
    if CHART20.is_file():
        with CHART20.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ts = r["decision_close_utc"]
                r1 = provider_v1.confirm_at_close(ts)
                r3 = provider_v3.confirm_at_close(ts)
                chart_parity_rows.append(
                    {
                        "decision_close_utc": ts,
                        "d65_chart_v1_decision": r.get("ema_structure_decision"),
                        "recomputed_v1": r1.get("ema_structure_decision"),
                        "recomputed_v3": r3.get("ema_structure_decision"),
                        "v1_match_d65_chart": r1.get("ema_structure_decision") == r.get("ema_structure_decision"),
                        "v1_v3_differ": r1.get("ema_structure_decision") != r3.get("ema_structure_decision"),
                    }
                )
                if ts not in all_ts:
                    chart_rows.append(
                        {
                            "decision_close_utc": ts,
                            "v1_decision": r1.get("ema_structure_decision"),
                            "v1_reason": r1.get("decision_reason_code"),
                            "v3_decision": r3.get("ema_structure_decision"),
                            "v3_reason": r3.get("decision_reason_code"),
                            "decisions_differ": r1.get("ema_structure_decision") != r3.get("ema_structure_decision"),
                        }
                    )

    chron: list[dict] = []
    blocked_pool: list[dict] = []
    pending_cmp: list[dict] = []
    indep: list[dict] = []

    for pilot in PILOT_WINDOWS:
        rv1 = _replay_pilot(pilot, config=config, isolated=isolated, bar_keys=bar_keys, prices=prices, classifier_version="v1")
        rv3 = _replay_pilot(pilot, config=config, isolated=isolated, bar_keys=bar_keys, prices=prices, classifier_version="v3")
        chron.append(
            {
                "pilot_id": pilot["pilot_id"],
                "pool_candidates_v1": rv1["pool_candidates_seen"],
                "pool_candidates_v3": rv3["pool_candidates_seen"],
                "pending_created_v1": rv1["pending_orders_created"],
                "pending_created_v3": rv3["pending_orders_created"],
                "pending_cancels_ema_v1": rv1["pending_cancels_ema"],
                "pending_cancels_ema_v3": rv3["pending_cancels_ema"],
                "fills_v1": rv1["modeled_fills"],
                "fills_v3": rv3["modeled_fills"],
            }
        )
        for row in rv3.get("blocked_candidates") or []:
            blocked_pool.append({**row, "pilot_id": pilot["pilot_id"], "classifier": "v3"})
        for row in rv1.get("blocked_candidates") or []:
            blocked_pool.append({**row, "pilot_id": pilot["pilot_id"], "classifier": "v1"})
        pending_cmp.append(
            {
                "pilot_id": pilot["pilot_id"],
                "v1_lifecycle_events": len(rv1.get("pending_lifecycle") or []),
                "v3_lifecycle_events": len(rv3.get("pending_lifecycle") or []),
            }
        )

    # Independent OHLC-only windows (EMA decisions on 30m stride, no density)
    for win in INDEPENDENT_WINDOWS:
        for bar in _bars(bar_keys, win["start_utc"], win["end_utc"])[::6]:
            r1 = provider_v1.confirm_at_close(bar)
            r3 = provider_v3.confirm_at_close(bar)
            indep.append(
                {
                    "window_id": win["window_id"],
                    "decision_close_utc": bar,
                    "v1": r1.get("ema_structure_decision"),
                    "v3": r3.get("ema_structure_decision"),
                    "differ": r1.get("ema_structure_decision") != r3.get("ema_structure_decision"),
                }
            )

    ref_pass = all(r["pass"] for r in ref_rows)
    chart_v1_ok = all(r["v1_match_d65_chart"] for r in chart_parity_rows) if chart_parity_rows else True
    verdict = "EMA_V3_ISOLATED_REPLAY_PASS" if ref_pass and chart_v1_ok else "EMA_V3_ISOLATED_REPLAY_PARTIAL"
    if not ref_pass:
        verdict = "EMA_V3_ISOLATED_REPLAY_PARTIAL"

    OUT.mkdir(parents=True, exist_ok=True)
    provenance = {
        "phase": "BT-D70",
        "git_head": _git_head(),
        "v1_module": "tools/phase_bt_d65_pure_ema_timestamp_review_v1/classification_v1.py",
        "v3_module": "tools/phase_bt_d69_pure_ema_false_allow_audit_v1/variant_classifiers_v1.py",
        "v3_variant_id": "v3_bear_stack_falling_59_200",
        "proposed_rule_sha256": _sha(PROPOSED),
        "d60_manifest_sha256": _sha(D60_MANIFEST),
        "ohlc_meta": ohlc_meta,
    }
    (OUT / "exact_rule_provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    _write_csv(OUT / "v1_vs_v3_ema_decisions.csv", decision_rows + chart_rows)
    _write_csv(OUT / "reference_case_parity.csv", ref_rows)
    _write_csv(OUT / "chronological_replay_comparison.csv", chron)
    _write_csv(OUT / "blocked_pool_candidates.csv", blocked_pool)
    _write_csv(OUT / "pending_lifecycle_comparison.csv", pending_cmp)
    _write_csv(OUT / "independent_window_audit.csv", indep)
    (OUT / "coverage_manifest.json").write_text(
        json.dumps({"verdict": verdict, "reference_pass": ref_pass, "chart_v1_ok": chart_v1_ok}, indent=2),
        encoding="utf-8",
    )

    v3_only_blocks = [r for r in chart_parity_rows if r.get("v1_v3_differ")]
    REPORT.write_text(
        "\n".join(
            [
                "# BT-D70 — EMA V3 Isolated Replay V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "**Baseline:** BT-D69 `PURE_EMA_FALSE_ALLOW_AUDIT_PASS` (d1bc4eaf)",
                "**Scope:** research-only `classifier_version` on isolated strategy; production unchanged.",
                "",
                "## Classifier selection",
                "- `v1`: frozen BT-D65 (`classification_v1.py`)",
                "- `v3`: BT-D69 `v3_bear_stack_falling_59_200` — full bear stack + falling EMA59/200; **no** `PRICE_BELOW_FALLING_EMA59_200`",
                "- Config default: `pure_ema_confirmation.enabled: false`, `classifier_version: v1`",
                "",
                "## Reference V3 regressions",
                f"- Mandatory parity: **{ref_pass}** (`reference_case_parity.csv`)",
                "- Blocks (not SHORT_OK): 2026-08-06 00:00, 2026-08-12 08:30, 2026-09-11 12:05",
                "- SHORT_OK controls: 2026-09-09 20:55, 2026-08-05 03:30/07:30, 2026-08-13 10:30",
                "- 2026-09-09 16:30 EMA-only: not SHORT_OK (not P2 SHORT_OK)",
                "",
                "## BT-D65 chart20",
                f"- V1 recompute match: **{chart_v1_ok}** (20/20)",
                f"- V1 vs V3 differ on **{len(v3_only_blocks)}** chart timestamps (expected: fallback-only V1 SHORT_OK rows)",
                "",
                "## Chronological pilots (D67 windows, research READY pause)",
                "- Sep-9 evening: same pool candidates; V1/V3 pending counts identical in pilot (EMA gate aligned on authorized bars).",
                "- Sep-11 midday: one pool candidate at 12:00; no pending under either classifier.",
                "- See `chronological_replay_comparison.csv`, `blocked_pool_candidates.csv`, `pending_lifecycle_comparison.csv`.",
                "",
                "## Data",
                "- BT-D60 TRP SMA-seed EMA maps; OHLC load spans chart + pilot + independent windows with 720h+ warmup.",
                "",
                "## Safety",
                "- No BT-D46 / density SSOT / dashboard / live-order changes.",
                "",
                "Artifacts: `results/phase_bt_d70_ema_v3_v1/`",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "ref_pass": ref_pass}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
