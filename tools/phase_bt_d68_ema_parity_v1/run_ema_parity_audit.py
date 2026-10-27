#!/usr/bin/env python3
"""BT-D68 — EMA replay parity and pending audit."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "results/phase_bt_d68_ema_parity_v1"
REPORT = ROOT / "reports/PHASE_BT_D68_EMA_PARITY_PENDING_AUDIT_V1.md"

sys.path.insert(0, str(PKG))

from false_allow_audit_v1 import run_false_allow_audit  # noqa: E402
from parity_audit_v1 import run_parity  # noqa: E402
from pending_timeline_v1 import build_timeline, sep11_explanation  # noqa: E402


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main() -> int:
    parity = run_parity()
    flat = parity["flat_comparison"]
    mismatches = [r for r in flat if not r["match"]]

    # Wide reference table per timestamp (pilot load profile)
    wide_rows = []
    for ts, per in parity["rows_by_ts"].items():
        row = per.get("bt_d67_pilot_load") or per.get("bt_d65_review_load") or {}
        wide_rows.append(
            {
                "decision_close_utc": ts,
                "close": row.get("close"),
                "ema9": row.get("ema9"),
                "ema20": row.get("ema20"),
                "ema59": row.get("ema59"),
                "ema200": row.get("ema200"),
                "close_below_ema20": row.get("close_below_ema20"),
                "close_below_ema59": row.get("close_below_ema59"),
                "ema59_slope_12b_pct": row.get("ema59_slope_12b_pct"),
                "ema200_slope_12b_pct": row.get("ema200_slope_12b_pct"),
                "ema_structure_decision": row.get("ema_structure_decision"),
                "decision_reason_code": row.get("decision_reason_code"),
                "d65_frozen_decision": (parity["d65_frozen_rows"].get(ts) or {}).get("ema_structure_decision"),
                "load_profiles_match": ts not in {m["decision_close_utc"] for m in mismatches},
            }
        )

    timeline = build_timeline()
    false_allow = run_false_allow_audit()

    OUT.mkdir(parents=True, exist_ok=True)
    _write_csv(OUT / "reference_ema_field_comparison.csv", flat)
    _write_csv(OUT / "pool_candidate_order_timeline.csv", timeline)
    _write_csv(OUT / "pure_ema_false_allow_audit.csv", false_allow)
    (OUT / "sep11_zero_pending_explanation.md").write_text(sep11_explanation(), encoding="utf-8")

    first_div_body = [
        "# First divergence (BT-D68)",
        "",
        parity["first_divergence"],
        "",
        "## Anomaly A (Sep-9 16:30)",
        "",
        "BT-D65 frozen CSV and BT-D67 pilot EMA path both yield **UNCERTAIN** (`FALLING_EMAS_PRICE_RECLAIM`).",
        "The historical **SHORT_OK** at 16:30 in BT-D62/D64 is **P2** (R5 pass ∧ ¬R6), not pure EMA.",
        "",
        "## OHLC / EMA parity",
        "",
        f"Cross-profile field mismatches at reference timestamps: **{len(mismatches)}**.",
        "",
        parity["ohlc_meta_note"],
    ]
    (OUT / "first_divergence.md").write_text("\n".join(first_div_body), encoding="utf-8")

    parity_resolved = len(mismatches) == 0
    verdict = "EMA_REPLAY_PARITY_RESOLVED" if parity_resolved else "EMA_REPLAY_PARITY_PARTIAL"

    manifest = {
        "phase": "BT-D68",
        "verdict": verdict,
        "git_head": _git_head(),
        "bt_d67_results": str(ROOT / "results/phase_bt_d67_canonical_ema_replay_v1"),
        "cross_profile_mismatches": len(mismatches),
        "anomaly_a_resolution": "P2_SHORT_OK_vs_EMA_UNCERTAIN_not_a_d67_regression",
        "anomaly_b_resolution": "no_pool_candidate_at_1205;_1200_pool_blocked_by_EMA_UNCERTAIN",
    }
    (OUT / "evidence_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    REPORT.write_text(
        "\n".join(
            [
                "# BT-D68 — EMA Parity & Pending Audit V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## A) Sep-9 16:30",
                "- Pure EMA: **UNCERTAIN** (D65 = D67 = D68 reload).",
                "- **SHORT_OK** in older tables is **P2 / R5–R6**, not BT-D65 EMA.",
                "",
                "## B) Sep-11 zero Pending",
                "- One pool candidate at **12:00** only; EMA **UNCERTAIN** → block.",
                "- **12:05** EMA **SHORT_OK** but **no** baseline pool signal → no Pending.",
                "",
                "See `results/phase_bt_d68_ema_parity_v1/sep11_zero_pending_explanation.md`.",
                "",
                "## Timing",
                "- BT-D67 `unresolved_timing_cases.csv` empty for pilots; runner order: **fill → EMA cancel → D41 review**.",
                "",
                "## Artifacts",
                "`results/phase_bt_d68_ema_parity_v1/`",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "mismatches": len(mismatches)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
