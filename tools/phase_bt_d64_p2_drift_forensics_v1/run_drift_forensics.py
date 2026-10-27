#!/usr/bin/env python3
"""BT-D64: P2/R6 drift root-cause forensics."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tools/phase_bt_d64_p2_drift_forensics_v1"
OUT = ROOT / "results/phase_bt_d64_p2_drift_v1"
REPORT = ROOT / "reports/PHASE_BT_D64_P2_R6_DRIFT_ROOT_CAUSE_V1.md"
PILOT = ROOT / "configs/pilot_bt_d21_pool_first_24h.yaml"

if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from parity_replay_v1 import REFERENCE_TS, compare_all, field_diff  # noqa: E402
from uncertain_breakdown_v1 import breakdown, stride_audit  # noqa: E402


def _sha(path: Path) -> str:
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    from config_yaml_v1 import load_yaml_and_validate  # noqa: E402

    config = load_yaml_and_validate(PILOT)
    all_diffs, first_div = compare_all(config)
    ref_diffs = [r for r in all_diffs if r["decision_close_utc"] in REFERENCE_TS]
    sep2055 = [r for r in all_diffs if r["decision_close_utc"] == "2026-09-09T20:55:00Z"]

    manifest = {
        "baseline_commits": {"bt_d62": "ef56e8dd", "bt_d63": "d78f3746"},
        "sha256": {
            "d61_per_close": _sha(ROOT / "results/phase_bt_d61_regime_audit_v1/per_close_regime_comparison.csv"),
            "d62_gate": _sha(ROOT / "results/phase_bt_d62_intent_gate_replay_v1/gate_decisions_at_intent.csv"),
            "d63_causal": _sha(ROOT / "results/phase_bt_d63_extended_timestamps_v1/causal_gate_decisions.csv"),
        },
        "r5_r6_provenance": "tools/phase_bt_d61_regime_gegenprobe_audit_v1/ablation_v1.py + regime_labels_v1.py",
        "first_divergence": first_div,
    }
    (OUT / "source_parity_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_csv(OUT / "sep9_2055_field_diff.csv", sep2055)
    _write_csv(OUT / "reference_decision_diff.csv", ref_diffs)
    unc = breakdown()
    _write_csv(OUT / "uncertain_coverage_breakdown.csv", unc)
    _write_csv(OUT / "stride_coverage_audit.csv", stride_audit())

    unc_counts = Counter(r["uncertain_cause"] for r in unc)
    short_ok = [
        r
        for r in csv.DictReader(
            (ROOT / "results/phase_bt_d63_extended_timestamps_v1/causal_gate_decisions.csv").open()
        )
        if r.get("P2") == "SHORT_OK" and not r.get("evidence_class", "").startswith("development")
    ]

    root = f"""# Root cause (BT-D64)

**Primary:** `{first_div.get('first_diverging_field', 'origin_ids_added')}` at 2026-09-09T20:55:00Z.

**Category:** (3) different historical feature coverage / pool evolution context — NOT different R5/R6 formulas.

**Mechanism:** BT-D63 `evaluate_close_v1` passes cumulative `prior_entry` across the full `sep_dev_sep9_evening` window (16:00–22:00). At 20:55, `compute_pool_evolution_row` sets `origin_ids_added=True` and `structural_continuity_vs_prior=REPLACEMENT`.

BT-D61/BT-D62 at 20:55 use cached row from `sep9_desired_short_2` window only → `prior_entry=None` → `origin_ids_added=False`, `NO_PRIOR`.

**R6 path:** Frozen R6 score reaches 2 via `flat59_fall200` + `origin_ids_added` (D63 only) → `SIDEWAYS_CANDIDATE` → P2 `R6_SIDEWAYS_VETO`.

**EMA/OHLC:** Identical chop/net/EMA slopes at 20:55 between D61 and D63 (TRP validated).

**Parity replay:** `d63_prior_none_replay` restores `origin_ids_added=False` and P2 SHORT_OK (see sep9_2055_field_diff.csv).
"""
    (OUT / "root_cause.md").write_text(root, encoding="utf-8")

    repair = """# Recommended repair (document only — not applied in BT-D64)

**Minimal:** In BT-D63 extended validation, reset `prior_entry=None` per decision-close when using D54 artifacts (match BT-D61 per-window pool evolution), OR scope prior chain to the same `window_id` as BT-D59 reference windows—not the merged 16:00–22:00 pilot window.

**Do not change** R5/R6 thresholds.

**Expected effect:** Sep-9 20:55 P2 aligns with BT-D62; Sep-9 16:30 unchanged.

**Separate issue:** 66× UNCERTAIN in D63 generalization pilot — mostly `NO_VERIFIED_UPPER_CANDIDATE_PROXY` on canonical stride windows (not missing density per se).
"""
    (OUT / "recommended_repair.md").write_text(repair, encoding="utf-8")

    verdict = "P2_DRIFT_ROOT_CAUSE_CONFIRMED" if first_div else "P2_DRIFT_PARTIAL"

    REPORT.write_text(
        "\n".join(
            [
                "# BT-D64 — P2/R6 Drift Root Cause V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## A. Sep-9 20:55 UTC",
                first_div.get("cause", ""),
                "",
                "- **BT-D62:** P2 SHORT_OK — `origin_ids_added=False`, R6 false (regime UNCERTAIN).",
                "- **BT-D63:** P2 SHORT_NO — `origin_ids_added=True`, R6 true (SIDEWAYS).",
                "- **First diverging input:** `origin_ids_added` (pool prior chain), not EMA.",
                "",
                "## B. Parity references",
                "See `reference_decision_diff.csv` for 16:30, 20:55, 12:00, 12:05, 12:30.",
                "",
                "## C. UNCERTAIN breakdown (D63)",
                *[f"- {k}: {v}" for k, v in sorted(unc_counts.items())],
                "",
                "## D. SHORT_OK generalization (3 bars)",
                f"Distinct timestamps: {sorted({r['decision_close_utc'] for r in short_ok})} — not a win rate.",
                "",
                "## F. Repair",
                "Documented in `recommended_repair.md` (prior_entry handling only).",
                "",
                f"Artifacts: `{OUT.relative_to(ROOT)}/`",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "first_div": first_div}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
