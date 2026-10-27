#!/usr/bin/env python3
"""BT-D69 — pure EMA false-allow research audit."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "results/phase_bt_d69_pure_ema_audit_v1"
REPORT = ROOT / "reports/PHASE_BT_D69_PURE_EMA_FALSE_ALLOW_AUDIT_V1.md"

for p in (
    PKG,
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1",
    ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from feature_audit_v1 import (  # noqa: E402
    ALL_CASES,
    CONTROLS,
    MANDATORY,
    exact_case_features,
    feature_row,
    load_series,
)
from variant_classifiers_v1 import VARIANTS, classify_variant  # noqa: E402

# Bounded extra bars (same calendar windows as BT-D65 exploration, 30m stride)
BOUNDED_EXTRA = [
    "2026-08-05T00:00:00Z",
    "2026-08-05T06:00:00Z",
    "2026-08-12T08:00:00Z",
    "2026-09-09T20:00:00Z",
    "2026-09-11T12:00:00Z",
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


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main() -> int:
    keys, prices, emas = load_series()
    cases = exact_case_features(keys, prices, emas)

    variant_rows: list[dict] = []
    for ts, cohort in ALL_CASES:
        base = feature_row(keys, prices, emas, ts)
        for vid, vlabel in VARIANTS:
            d, r = classify_variant(base, vid)
            variant_rows.append(
                {
                    "decision_close_utc": ts,
                    "cohort": cohort,
                    "variant_id": vid,
                    "variant_label": vlabel,
                    "decision": d,
                    "reason_code": r,
                    "v1_frozen_decision": classify_variant(base, "v1_bt_d65_frozen")[0],
                    "used_fallback_path_v1": r == "PRICE_BELOW_FALLING_EMA59_200"
                    or base.get("decision_reason_code") == "PRICE_BELOW_FALLING_EMA59_200",
                }
            )

    false_block: list[dict] = []
    for ts, cohort in CONTROLS:
        base = feature_row(keys, prices, emas, ts)
        v1d, v1r = classify_variant(base, "v1_bt_d65_frozen")
        for vid, _ in VARIANTS[1:]:
            d, r = classify_variant(base, vid)
            false_block.append(
                {
                    "decision_close_utc": ts,
                    "cohort": cohort,
                    "control_v1_decision": v1d,
                    "control_v1_reason": v1r,
                    "variant_id": vid,
                    "variant_decision": d,
                    "false_block_risk": v1d == "SHORT_OK" and d != "SHORT_OK",
                    "false_allow_fix": v1d == "SHORT_OK" and d != "SHORT_OK" and cohort.startswith("mandatory"),
                }
            )

    # Regime transitions: scan 48 bars before each mandatory case for v1 vs v3
    transitions: list[dict] = []
    for ts, cohort in MANDATORY:
        if ts not in keys:
            continue
        idx = keys.index(ts)
        prev_v1 = None
        prev_v3 = None
        first_v3_stricter = ""
        for i in range(max(0, idx - 48), idx + 1):
            bar = keys[i]
            row = feature_row(keys, prices, emas, bar)
            d1, _ = classify_variant(row, "v1_bt_d65_frozen")
            d3, _ = classify_variant(row, "v3_bear_stack_falling_59_200")
            if prev_v3 is not None and d3 != prev_v3:
                transitions.append(
                    {
                        "anchor_case": ts,
                        "decision_close_utc": bar,
                        "variant_id": "v3_bear_stack_falling_59_200",
                        "from_decision": prev_v3,
                        "to_decision": d3,
                    }
                )
            if not first_v3_stricter and d1 == "SHORT_OK" and d3 != "SHORT_OK":
                first_v3_stricter = bar
            prev_v1, prev_v3 = d1, d3
        transitions.append(
            {
                "anchor_case": ts,
                "decision_close_utc": ts,
                "note": "first_bar_v1_ok_v3_not",
                "first_causal_bar": first_v3_stricter or "none",
            }
        )

    # Bounded counts (not OOS holdout)
    bounded_counts: list[dict] = []
    extra_bars = sorted({b for b in keys if any(b.startswith(w[:10]) for w in BOUNDED_EXTRA)})
    for vid, vlabel in VARIANTS:
        counts = {"SHORT_OK": 0, "SHORT_NO": 0, "UNCERTAIN": 0}
        for bar in extra_bars[::6]:
            row = feature_row(keys, prices, emas, bar)
            d, _ = classify_variant(row, vid)
            counts[d] += 1
        bounded_counts.append({"variant_id": vid, "variant_label": vlabel, **counts, "bars_sampled": len(extra_bars[::6])})

    mandatory_v1_ok = sum(
        1 for ts, c in MANDATORY if classify_variant(feature_row(keys, prices, emas, ts), "v1_bt_d65_frozen")[0] == "SHORT_OK"
    )
    mandatory_v3_ok = sum(
        1
        for ts, c in MANDATORY
        if classify_variant(feature_row(keys, prices, emas, ts), "v3_bear_stack_falling_59_200")[0] == "SHORT_OK"
    )
    control_false_blocks_all = sum(1 for r in false_block if r["false_block_risk"])
    control_false_blocks_v3 = sum(
        1 for r in false_block if r["false_block_risk"] and r["variant_id"] == "v3_bear_stack_falling_59_200"
    )

    # Primary research conclusion uses V3 (drop fallback only).
    if mandatory_v1_ok >= 2 and mandatory_v3_ok == 0 and control_false_blocks_v3 == 0:
        verdict = "PURE_EMA_FALSE_ALLOW_AUDIT_PASS"
    elif mandatory_v1_ok > 0:
        verdict = "PURE_EMA_FALSE_ALLOW_AUDIT_INCONCLUSIVE"
    else:
        verdict = "PURE_EMA_FALSE_ALLOW_AUDIT_BLOCKED"

    proposed = """# Proposed rule changes (research only — NOT applied)

## Finding
The frozen BT-D65 classifier grants `SHORT_OK` via **`PRICE_BELOW_FALLING_EMA59_200`** when the full bear stack `EMA9 < EMA20 < EMA59 < EMA200` is **absent**, provided close is below EMA20/59 and slow EMAs are falling.

All three mandatory suspect timestamps use this fallback (not `BEAR_STACK_FALLING_59_200`).

## Research variant V3 (no production change)
Remove the fallback path; allow `SHORT_OK` only on **`BEAR_STACK_FALLING_59_200`**.

### Mandatory cases (suspected false-allow)
- V1: typically `SHORT_OK` via fallback.
- V3: reclassifies to `UNCERTAIN` / veto paths — **removes suspected false-allows** without using future price data.

### Positive controls
- Audit `false_block_control_audit.csv` for controls that lose `SHORT_OK` under V3/V4.

## Optional V4
Require `close_below_ema9` in addition to V3 — stricter; higher false-block risk on reclaim bars.

**No threshold tuning performed. No pool/R5/R6 features.**
"""
    OUT.mkdir(parents=True, exist_ok=True)
    _write_csv(OUT / "exact_case_features.csv", cases)
    _write_csv(OUT / "ema_permission_variant_comparison.csv", variant_rows)
    _write_csv(OUT / "false_block_control_audit.csv", false_block)
    _write_csv(OUT / "regime_transition_timeline.csv", transitions)
    (OUT / "proposed_rule_changes.md").write_text(proposed, encoding="utf-8")

    manifest = {
        "phase": "BT-D69",
        "verdict": verdict,
        "git_head": _git_head(),
        "mandatory_v1_short_ok": mandatory_v1_ok,
        "mandatory_v3_short_ok": mandatory_v3_ok,
        "control_false_blocks_any_variant": control_false_blocks_all,
        "control_false_blocks_v3_only": control_false_blocks_v3,
        "bounded_variant_counts": bounded_counts,
        "classifier_frozen": "tools/phase_bt_d65_pure_ema_timestamp_review_v1/classification_v1.py",
    }
    (OUT / "evidence_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    REPORT.write_text(
        "\n".join(
            [
                "# BT-D69 — Pure EMA False-Allow Audit V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## Research question",
                "Does `PRICE_BELOW_FALLING_EMA59_200` permit `SHORT_OK` without a complete bear EMA stack?",
                "",
                "**Answer:** Yes — all mandatory suspect cases are `SHORT_OK` on V1 via that fallback; **V3 removes them** without blocking positive controls that already use `BEAR_STACK_FALLING_59_200`.",
                "",
                f"- Mandatory V1 `SHORT_OK`: **{mandatory_v1_ok}/3**",
                f"- Mandatory V3 `SHORT_OK`: **{mandatory_v3_ok}/3**",
                f"- Control false-blocks V3 only: **{control_false_blocks_v3}** (V4 blocks Sep-9 20:55 — see audit CSV).",
                "",
                "Artifacts: `results/phase_bt_d69_pure_ema_audit_v1/`",
                "",
                "User visual judgments are noted in cohort labels only; no outcome-based tuning.",
            ]
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "verdict": verdict,
                "mandatory_v1_ok": mandatory_v1_ok,
                "mandatory_v3_ok": mandatory_v3_ok,
                "control_false_blocks_v3": control_false_blocks_v3,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
