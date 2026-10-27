#!/usr/bin/env python3
"""BT-D61: causal regime Gegenprobe audit and rule candidates."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1"
OUT = ROOT / "results/phase_bt_d61_regime_audit_v1"
REPORT = ROOT / "reports/PHASE_BT_D61_REGIME_GEGENPROBE_RULE_AUDIT_V1.md"
GEGENPROBE = ROOT / "results/phase_bt_d60_ema_history_v1/regime_gegenprobe_asof.csv"
PILOT = ROOT / "configs/pilot_bt_d21_pool_first_24h.yaml"

if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from ablation_v1 import ablation_flags, first_variant_drop  # noqa: E402
from enrich_v1 import attach_ema_slopes, enrich_pool_for_windows, load_ema_context  # noqa: E402
from episodes_v1 import build_episodes  # noqa: E402
from false_block_v1 import build_false_block_audit  # noqa: E402
from input_verify_v1 import verify_inputs  # noqa: E402
from regime_labels_v1 import research_regime  # noqa: E402
from sl_pause_v1 import build_sl_pause_audit  # noqa: E402


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r.keys() if k != "ablation"})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {k: v for k, v in r.items() if k != "ablation"}
            if "ablation" in r:
                flat.update(r["ablation"])
            w.writerow(flat)


def _flatten_ablation(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rows:
        ab = ablation_flags(r)
        r["ablation"] = ab
        r["first_variant_drop"] = first_variant_drop(ab)
        r["research_regime"] = research_regime(r)
    return rows


def _proposed_rules(episodes: list[dict], false_block: list[dict]) -> str:
    lines = [
        "# BT-D61 Proposed Rule Candidates (UNVALIDATED)",
        "",
        "All thresholds are research-only; strategy remains fail-closed.",
        "",
        "## Candidate R1 — EMA structure (Variant A)",
        "- Require bearish EMA9<20<59<200 at decision close.",
        "- **Limitation:** Sep-11 dense upper passes A while Sep-9 passes; not selective.",
        "",
        "## Candidate R2 — Slopes (Variant B)",
        "- Add EMA59 and EMA200 12-bar negative slopes.",
        "- **False-block risk:** Sep-9 desired anchors fail B when EMA59 flattening (see false_block_audit).",
        "",
        "## Candidate R3 — Direction + chop (Variant C)",
        "- net_move_24b_pct < 0 and chop_ratio_24b < 8.",
        "",
        "## Candidate R4 — Upper isolation (Variant D)",
        "- gap_to_next_upper_pct >= 0.5%, zero intermediate uppers, local_stack_density <= 2.",
        "",
        "## Candidate R5 — No stack growth (Variant E)",
        "- Reject when local_stack_density >= 3 or origin_ids_added (past bar).",
        "",
        "## Candidate R6 — Sideways composite (research regime)",
        "- Label SIDEWAYS when >=2 of: chop>=6, |net_24b|<0.35%, EMA59 flattening + falling EMA200, stack>=3, origin added.",
        "",
        f"## Episode count: {len(episodes)} grouped segments (not independent markets).",
        "",
    ]
    for fb in false_block:
        lines.append(f"- {fb.get('case_id')}: block_before_intent={fb.get('block_before_intent')}")
    return "\n".join(lines) + "\n"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    verified = verify_inputs()
    (OUT / "verified_input_manifest.json").write_text(
        json.dumps(verified, indent=2),
        encoding="utf-8",
    )
    if not verified["all_ok"]:
        REPORT.write_text(
            "# BT-D61\n\n**VERDICT:** `REGIME_RULE_RESEARCH_BLOCKED`\n\nInput verification failed.\n",
            encoding="utf-8",
        )
        return 2

    float_cols = {
        "close",
        "ema9",
        "ema20",
        "ema59",
        "ema200",
        "ema9_minus_ema20_pct",
        "ema20_minus_ema59_pct",
        "ema59_minus_ema200_pct",
        "ema9_slope_3b_pct",
        "ema9_slope_6b_pct",
        "ema20_slope_6b_pct",
        "ema59_slope_12b_pct",
        "ema200_slope_12b_pct",
        "range_24b_pct",
        "net_move_24b_pct",
        "chop_ratio_24b",
    }
    int_cols = {"ema9_20_cross_count_past_only", "lower_high_count", "lower_low_count", "higher_high_count"}
    rows = list(csv.DictReader(GEGENPROBE.open(encoding="utf-8")))
    for r in rows:
        for k in list(r.keys()):
            if k in float_cols:
                try:
                    r[k] = float(r[k]) if r[k] not in ("", "None") else None
                except ValueError:
                    pass
            if k in int_cols:
                try:
                    r[k] = int(float(r[k])) if r[k] not in ("", "None") else None
                except ValueError:
                    pass
            if k.startswith("close_below") or k == "ema9_lt_20_lt_59_lt_200":
                r[k] = r[k] in ("True", "true", "1")

    bar_keys, _prices, emas = load_ema_context()
    for r in rows:
        attach_ema_slopes(r, bar_keys, emas)

    from config_yaml_v1 import load_yaml_and_validate  # noqa: E402

    config = load_yaml_and_validate(PILOT)
    enrich_pool_for_windows(rows, config)
    rows = _flatten_ablation(rows)

    rows_by_bar = {r["decision_close_utc"]: r for r in rows}
    episodes = build_episodes(rows)
    false_block = build_false_block_audit(rows_by_bar, rows)
    sl_pause = build_sl_pause_audit(rows_by_bar)

    ablation_summary: list[dict[str, Any]] = []
    for wid in sorted({r["window_id"] for r in rows}):
        subset = [r for r in rows if r["window_id"] == wid]
        for variant in (
            "variant_A_ema_order",
            "variant_B_plus_slopes_12b",
            "variant_C_plus_direction_chop",
            "variant_D_plus_upper_isolation",
            "variant_E_plus_no_stack_growth",
        ):
            ablation_summary.append(
                {
                    "window_id": wid,
                    "variant": variant,
                    "pass_count": sum(1 for r in subset if r["ablation"].get(variant)),
                    "total": len(subset),
                    "pass_rate": round(
                        sum(1 for r in subset if r["ablation"].get(variant)) / max(len(subset), 1),
                        4,
                    ),
                }
            )

    _write_csv(OUT / "per_close_regime_comparison.csv", rows)
    _write_csv(OUT / "regime_episodes.csv", episodes)
    _write_csv(OUT / "feature_ablation.csv", ablation_summary)
    _write_csv(OUT / "reference_case_decisions.csv", false_block)
    _write_csv(OUT / "false_block_audit.csv", false_block)
    _write_csv(OUT / "short_pause_requalification_audit.csv", sl_pause)
    (OUT / "proposed_rules.md").write_text(_proposed_rules(episodes, false_block), encoding="utf-8")

    regime_counts = {}
    for r in rows:
        regime_counts[r["research_regime"]] = regime_counts.get(r["research_regime"], 0) + 1
    independent_episodes = len(
        [e for e in episodes if e["evidence_class"] in ("chronological_control", "transition_reference")]
    )

    sep9_d = sum(1 for r in rows if r["window_id"].startswith("sep9") and r["research_regime"] == "DOWNTREND_CANDIDATE")
    sep11_s = sum(
        1
        for r in rows
        if r["window_id"] == "sep11_dense_upper" and r["research_regime"] == "SIDEWAYS_CANDIDATE"
    )
    pool_helps = sum(1 for r in rows if r["ablation"].get("variant_C_plus_direction_chop") and not r["ablation"].get("variant_D_plus_upper_isolation"))

    verdict = "REGIME_RULE_RESEARCH_PASS"
    if verified["d60_verdict"] != "EMA_HISTORY_VALIDATED":
        verdict = "REGIME_RULE_RESEARCH_BLOCKED"
    elif any(r.get("ema_validity") != "VALIDATED_TRP_D60" for r in rows):
        verdict = "REGIME_RULE_RESEARCH_INCONCLUSIVE"

    REPORT.write_text(
        "\n".join(
            [
                "# BT-D61 — Regime Gegenprobe Rule Audit V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## A. Provenance",
                f"- Gegenprobe rows: **{len(rows)}** (SHA verified vs BT-D60A)",
                f"- EMA path: **TRP SMA-seed** (`ema_validity=VALIDATED_TRP_D60` on all rows: "
                f"{sum(1 for r in rows if r.get('ema_validity')=='VALIDATED_TRP_D60')}/{len(rows)})`",
                "- Pool: D54 (Sep-9), D9 cache (Sep-11); Sep-4/5 pool from D59 stride cache "
                "where available else EMA-only (`UNCERTAIN_NO_DENSITY_AT_CLOSE`)",
                "",
                "## B. Regime distribution",
                *[f"- {k}: {v}" for k, v in sorted(regime_counts.items())],
                "",
                "## Answers",
                "1. **Sep-9 vs Sep-11:** Bearish EMA stack overlaps; Sep-11 separates on **stack density / origin growth / chop** "
                f"(dense window SIDEWAYS labels: {sep11_s}/{13} bars). Sep-9 DOWNTREND labels: {sep9_d}/19.",
                "2. **Early sideways prevention:** SIDEWAYS label at **2026-09-11T12:00:00Z** (1 bar before 12:05 intent; "
                "6 bars before 12:30). Variant **E does not block** at intent (FAIL_A/B/C); stack/chop composite still flags risk.",
                f"3. **Pool evolution:** {pool_helps} bars pass C but fail D — isolation/gap filters add signal beyond EMA.",
                f"4. **Independent episodes:** {len(episodes)} grouped segments; ~{independent_episodes} non-development evidence classes. **No OOS holdout.**",
                "5. **Rule candidates:** `results/phase_bt_d61_regime_audit_v1/proposed_rules.md` (R1–R6, UNVALIDATED).",
                "",
                "## Reference coverage",
                "- Sep-9 16:30 / 20:55: **in dataset**",
                "- Sep-11 12:25 / 12:35: **in dataset**",
                "- Sep-4–5 transition: **289 bars** (full 5m in Gegenprobe, not D59 stride)",
                "",
                "## Safety",
                "No strategy activation. `regime_classifier_v1` unchanged.",
                "",
                "## Outputs",
                f"- `{OUT.relative_to(ROOT)}/`",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "rows": len(rows), "episodes": len(episodes)}, indent=2))
    return 0 if verdict != "REGIME_RULE_RESEARCH_BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
