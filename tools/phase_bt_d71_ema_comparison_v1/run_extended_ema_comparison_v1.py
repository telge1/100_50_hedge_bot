#!/usr/bin/env python3
"""BT-D71 — extended real-pool EMA V1 vs V3 chronological comparison."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "results/phase_bt_d71_ema_comparison_v1"
REPORT = ROOT / "reports/PHASE_BT_D71_EXTENDED_EMA_V1_V3_REPLAY_V1.md"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"
D67 = ROOT / "tools/phase_bt_d67_canonical_ema_replay_v1"
D60_MANIFEST = ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json"

for p in (
    PKG,
    ISOLATED,
    D67,
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from compare_chronology_v1 import (  # noqa: E402
    build_close_comparison_rows,
    fill_exit_rows,
    pending_difference_rows,
)
from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from pilot_density_session_v1 import build_pilot_density_session, preflight_coverage  # noqa: E402
from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1  # noqa: E402
from replay_windows_v1 import ALL_REPLAY_WINDOWS, MANDATORY_EMA_AUDIT  # noqa: E402
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402


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


def _replay(
    *,
    pilot: dict,
    config: dict,
    isolated: dict,
    bar_keys: list[str],
    prices: dict,
    classifier_version: str,
    pause_ready: bool,
    session: Any | None = None,
) -> dict:
    session = session or build_pilot_density_session(config, pilot)
    bars = _bars(bar_keys, pilot["start_utc"], pilot["end_utc"])
    iso = dict(isolated)
    iso["pure_ema_confirmation"] = {
        **(isolated.get("pure_ema_confirmation") or {}),
        "enabled": True,
        "classifier_version": classifier_version,
    }
    gate = build_pure_ema_gate(bar_keys=bar_keys, prices=prices, isolated_strategy_config=iso)
    pause = ShortPauseControllerV1()
    if pause_ready:
        pause.set_ready_for_research_only()
    replay = run_isolated_chronological_replay(
        config=config,
        isolated_bundle=iso,
        prices=prices,
        bars=bars,
        density_session=session,
        pause=pause,
        ema_gate=gate,
    )
    replay["pilot_id"] = pilot["pilot_id"]
    replay["classifier_version"] = classifier_version
    replay["pause_mode"] = "RESEARCH_READY" if pause_ready else "DEFAULT_SHORT_PAUSED"
    replay["bars"] = bars
    replay["density_session"] = session
    return replay


def _ema_map(provider: PureEmaSeriesProviderV1, bars: list[str]) -> dict[str, dict]:
    return {b: provider.confirm_at_close(b) for b in bars}


def main() -> int:
    if not D60_MANIFEST.is_file():
        raise SystemExit("BT-D60 manifest missing")
    d60 = json.loads(D60_MANIFEST.read_text())
    if d60.get("verdict") != "EMA_HISTORY_VALIDATED":
        raise SystemExit("BT-D60 not validated")

    bundle = load_isolated_strategy_bundle()
    isolated_base = dict(bundle["isolated_strategy_v1"])
    isolated_base["pure_ema_confirmation"] = {
        **(isolated_base.get("pure_ema_confirmation") or {}),
        "enabled": True,
    }
    isolated_base["isolated_upper_eligibility"] = {
        **(isolated_base.get("isolated_upper_eligibility") or {}),
        "research_pass_on_baseline_signal": True,
    }
    config = bundle["baseline_config"]

    coverage_starts = [w["start_utc"] for w in ALL_REPLAY_WINDOWS]
    coverage_ends = [w["end_utc"] for w in ALL_REPLAY_WINDOWS]
    for a in MANDATORY_EMA_AUDIT:
        coverage_starts.append(a["decision_close_utc"])
        coverage_ends.append(a["decision_close_utc"])
    load_start = _parse_utc(min(coverage_starts)) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(max(coverage_ends))
    bar_keys, prices, ohlc_meta = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))

    provider_v1 = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices, classifier_version="v1")
    provider_v3 = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices, classifier_version="v3")

    coverage_rows: list[dict] = []
    preflight_blocked = False

    def _preflight_sample(session: Any, bars: list[str]) -> list[dict]:
        if not bars:
            return []
        idxs = {0, len(bars) // 2, len(bars) - 1}
        sample = [bars[i] for i in sorted(idxs)]
        return preflight_coverage(session, sample)

    for pilot in ALL_REPLAY_WINDOWS:
        session = build_pilot_density_session(config, pilot)
        bars = _bars(bar_keys, pilot["start_utc"], pilot["end_utc"])
        pf = _preflight_sample(session, bars)
        incomplete = sum(1 for r in pf if r["coverage_status"] != "OK")
        if incomplete == len(pf) and pf:
            preflight_blocked = True
        for row in pf:
            coverage_rows.append(
                {
                    **row,
                    "pilot_id": pilot["pilot_id"],
                    "preflight_scope": "sample_start_mid_end",
                }
            )

    if preflight_blocked:
        verdict = "EXTENDED_EMA_REPLAY_BLOCKED"
    else:
        verdict = "EXTENDED_EMA_REPLAY_PASS"

    close_rows: list[dict] = []
    pool_candidates: list[dict] = []
    pending_diffs: list[dict] = []
    positions_v1: list[dict] = []
    positions_v3: list[dict] = []
    fill_cmp: list[dict] = []
    pause_impact: list[dict] = []
    unresolved: list[dict] = []
    mandatory_audit: list[dict] = []

    pilots_ok = not preflight_blocked

    for pilot in ALL_REPLAY_WINDOWS:
        bars = _bars(bar_keys, pilot["start_utc"], pilot["end_utc"])
        if not bars:
            unresolved.append({"pilot_id": pilot["pilot_id"], "issue": "NO_OHLC_BARS"})
            pilots_ok = False
            continue

        session = build_pilot_density_session(config, pilot)
        print(f"BT-D71 replay pilot={pilot['pilot_id']} bars={len(bars)}", flush=True)
        rv1_ready = _replay(
            pilot=pilot,
            config=config,
            isolated=isolated_base,
            bar_keys=bar_keys,
            prices=prices,
            classifier_version="v1",
            pause_ready=True,
            session=session,
        )
        rv3_ready = _replay(
            pilot=pilot,
            config=config,
            isolated=isolated_base,
            bar_keys=bar_keys,
            prices=prices,
            classifier_version="v3",
            pause_ready=True,
            session=session,
        )
        rv1_pause = _replay(
            pilot=pilot,
            config=config,
            isolated=isolated_base,
            bar_keys=bar_keys,
            prices=prices,
            classifier_version="v1",
            pause_ready=False,
            session=session,
        )

        ema_v1 = _ema_map(provider_v1, bars)
        ema_v3 = _ema_map(provider_v3, bars)
        pilot_close = build_close_comparison_rows(
            pilot_id=pilot["pilot_id"],
            pause_mode="RESEARCH_READY",
            bars=bars,
            replay_v1=rv1_ready,
            replay_v3=rv3_ready,
            ema_rows_v1=ema_v1,
            ema_rows_v3=ema_v3,
        )
        close_rows.extend(pilot_close)
        pending_diffs.extend(pending_difference_rows(pilot_close))

        for row in rv1_ready.get("baseline_pool_candidates") or []:
            pool_candidates.append({**row, "pilot_id": pilot["pilot_id"]})
        positions_v1.extend(fill_exit_rows(rv1_ready, classifier="v1", pilot_id=pilot["pilot_id"]))
        positions_v3.extend(fill_exit_rows(rv3_ready, classifier="v3", pilot_id=pilot["pilot_id"]))

        for cls, rv in (("v1", rv1_ready), ("v3", rv3_ready)):
            fill_cmp.append(
                {
                    "pilot_id": pilot["pilot_id"],
                    "classifier": cls,
                    "pause_mode": "RESEARCH_READY",
                    "pending_created": rv["pending_orders_created"],
                    "pending_cancels_ema": rv["pending_cancels_ema"],
                    "modeled_fills": rv["modeled_fills"],
                    "pool_candidates_seen": rv["pool_candidates_seen"],
                }
            )

        pause_impact.append(
            {
                "pilot_id": pilot["pilot_id"],
                "classifier": "v1",
                "pause_mode": rv1_pause["pause_mode"],
                "pending_created": rv1_pause["pending_orders_created"],
                "pool_candidates_seen": rv1_pause["pool_candidates_seen"],
                "pause_state_final": rv1_pause["pause_state_final"],
                "note": "EMA classifier irrelevant when SHORT_PAUSED blocks all intents",
            }
        )
        pause_impact.append(
            {
                "pilot_id": pilot["pilot_id"],
                "classifier": "v1",
                "pause_mode": rv1_ready["pause_mode"],
                "pending_created": rv1_ready["pending_orders_created"],
                "pool_candidates_seen": rv1_ready["pool_candidates_seen"],
                "pause_state_final": rv1_ready["pause_state_final"],
            }
        )
        pause_impact.append(
            {
                "pilot_id": pilot["pilot_id"],
                "classifier": "v3",
                "pause_mode": rv3_ready["pause_mode"],
                "pending_created": rv3_ready["pending_orders_created"],
                "pool_candidates_seen": rv3_ready["pool_candidates_seen"],
                "pause_state_final": rv3_ready["pause_state_final"],
            }
        )

        if (rv1_ready.get("coverage_incomplete") or []) or (rv3_ready.get("coverage_incomplete") or []):
            unresolved.append(
                {
                    "pilot_id": pilot["pilot_id"],
                    "issue": "DENSITY_COLLECT_GAPS",
                    "v1_gaps": len(rv1_ready.get("coverage_incomplete") or []),
                    "v3_gaps": len(rv3_ready.get("coverage_incomplete") or []),
                }
            )

    for audit in MANDATORY_EMA_AUDIT:
        ts = audit["decision_close_utc"]
        e1 = provider_v1.confirm_at_close(ts)
        e3 = provider_v3.confirm_at_close(ts)
        v1_dec = e1.get("ema_structure_decision")
        v3_dec = e3.get("ema_structure_decision")
        v3_blocks = v3_dec != "SHORT_OK"
        pool_at = [r for r in pool_candidates if r["decision_close_utc"] == ts]
        close_at = [r for r in close_rows if r["decision_close_utc"] == ts and r["pause_mode"] == "RESEARCH_READY"]
        mandatory_audit.append(
            {
                "audit_type": "MANDATORY_EMA_POOL",
                **audit,
                "actual_v1_ema": v1_dec,
                "actual_v3_ema": v3_dec,
                "v3_blocks": v3_blocks,
                "canonical_pool_candidate_at_close": bool(pool_at),
                "pool_key": pool_at[0].get("pool_key") if pool_at else "",
                "v1_would_authorize_pending": close_at[0].get("v1_would_authorize_pending") if close_at else None,
                "v3_would_authorize_pending": close_at[0].get("v3_would_authorize_pending") if close_at else None,
                "v1_pending_open_after": close_at[0].get("v1_pending_open_end") if close_at else None,
                "v3_pending_open_after": close_at[0].get("v3_pending_open_end") if close_at else None,
            }
        )

    ema_diff_bars = [r for r in close_rows if r.get("ema_only_differs")]
    pool_cond_diff = [r for r in close_rows if r.get("pool_conditional_differs")]
    pending_chrono_diff = [
        r
        for r in close_rows
        if r.get("pause_mode") == "RESEARCH_READY" and r.get("v1_pending_open_end") != r.get("v3_pending_open_end")
    ]

    any_fill_diff = any(
        a.get("modeled_fills") != b.get("modeled_fills")
        for a, b in (
            (x, y)
            for x in fill_cmp
            for y in fill_cmp
            if x["pilot_id"] == y["pilot_id"] and x["classifier"] == "v1" and y["classifier"] == "v3"
        )
    )
    any_pending_diff = any(
        a.get("pending_created") != b.get("pending_created")
        for a, b in (
            (x, y)
            for x in fill_cmp
            for y in fill_cmp
            if x["pilot_id"] == y["pilot_id"] and x["classifier"] == "v1" and y["classifier"] == "v3"
        )
    )

    if pilots_ok and verdict != "EXTENDED_EMA_REPLAY_BLOCKED":
        if not mandatory_audit or not all(
            r["actual_v1_ema"] == "SHORT_OK" and r["v3_blocks"] for r in mandatory_audit
        ):
            verdict = "EXTENDED_EMA_REPLAY_PARTIAL"
        elif unresolved:
            verdict = "EXTENDED_EMA_REPLAY_PARTIAL"

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "phase": "BT-D71",
        "baseline_commit": "5c18c0cd",
        "git_head": _git_head(),
        "d60_manifest_sha256": _sha(D60_MANIFEST),
        "ohlc_load_start": _iso(load_start),
        "ohlc_load_end": _iso(load_end),
        "ohlc_meta": ohlc_meta,
        "windows": [w["pilot_id"] for w in ALL_REPLAY_WINDOWS],
        "verdict": verdict,
        "verified_evaluation_bars": len(close_rows),
        "pool_eligible_closes": sum(1 for r in close_rows if r.get("valid_pool_candidate")),
        "ema_only_diff_bars": len(ema_diff_bars),
        "pool_conditional_diff_bars": len(pool_cond_diff),
        "pending_state_diff_bars": len(pending_chrono_diff),
        "any_pending_created_diff": any_pending_diff,
        "any_modeled_fill_diff": any_fill_diff,
    }
    (OUT / "input_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_csv(OUT / "canonical_coverage.csv", coverage_rows)
    _write_csv(OUT / "ema_v1_vs_v3_at_closes.csv", close_rows)
    _write_csv(OUT / "valid_pool_candidates.csv", pool_candidates)
    _write_csv(OUT / "actual_pending_differences.csv", pending_diffs)
    _write_csv(OUT / "chronological_positions_v1.csv", positions_v1)
    _write_csv(OUT / "chronological_positions_v3.csv", positions_v3)
    _write_csv(OUT / "fill_exit_comparison.csv", fill_cmp)
    _write_csv(OUT / "pause_impact.csv", pause_impact)
    for row in unresolved:
        row.setdefault("audit_type", "COVERAGE_GAP")
    _write_csv(OUT / "unresolved_cases.csv", unresolved + mandatory_audit)

    v3_changes_orders = bool(any_pending_diff or any_fill_diff)
    v3_classification_only = bool(pool_cond_diff) and not v3_changes_orders

    REPORT.write_text(
        "\n".join(
            [
                "# BT-D71 — Extended EMA V1 vs V3 Real Pool Replay",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## Question",
                "Does EMA V3 change historical short **orders** and modeled outcomes vs frozen V1,",
                "given identical canonical pool inputs and unchanged Entry/SL/TP/pending rules?",
                "",
                "## Summary (research-ready pause track)",
                f"- Verified evaluation closes: **{manifest['verified_evaluation_bars']}**",
                f"- Closes with valid pool candidate: **{manifest['pool_eligible_closes']}**",
                f"- EMA-only classification differs: **{manifest['ema_only_diff_bars']}** closes",
                f"- Pool-conditional authorize differs (V1 vs V3): **{manifest['pool_conditional_diff_bars']}** closes",
                f"- Pending open state differs end-of-bar: **{manifest['pending_state_diff_bars']}** closes",
                f"- Any pilot pending_created diff V1 vs V3: **{any_pending_diff}**",
                f"- Any pilot modeled_fills diff V1 vs V3: **{any_fill_diff}**",
                "",
                "## Mandatory EMA audit (pool + EMA)",
                "See `unresolved_cases.csv` (mandatory audit rows) for per-timestamp pool presence and pending impact.",
                "",
                "### Interpretation",
                "- V3 changes **classification** on multiple bars (fallback removal).",
                "- **Trade chronology** changes only where a pool candidate coincides with V1-only EMA permission.",
                f"- V3 changes modeled pending/fills vs V1: **{v3_changes_orders}**",
                f"- EMA-only diffs without order change possible: **{v3_classification_only}**",
                "- No win-rate or PnL improvement claim (insufficient independent samples).",
                "",
                "## Pause (fail-closed default)",
                "Separate `pause_impact.csv`: `DEFAULT_SHORT_PAUSED` blocks all pending creation;",
                "labeled `RESEARCH_READY` runs isolate EMA V1/V3 chronology.",
                "",
                "## Coverage",
                "Canonical / D54 / D9 cache layered per window. Missing density bars recorded;",
                "no fabricated pool candidates.",
                "",
                "Artifacts: `results/phase_bt_d71_ema_comparison_v1/`",
            ]
        ),
        encoding="utf-8",
    )

    print(json.dumps(manifest, indent=2))
    return 0 if verdict != "EXTENDED_EMA_REPLAY_BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
