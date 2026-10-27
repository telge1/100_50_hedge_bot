#!/usr/bin/env python3
"""BT-D62: causal Short intent gate replay at real PENDING_CREATE times."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tools/phase_bt_d62_intent_gate_replay_v1"
OUT = ROOT / "results/phase_bt_d62_intent_gate_replay_v1"
REPORT = ROOT / "reports/PHASE_BT_D62_CAUSAL_INTENT_GATE_REPLAY_V1.md"
PILOT = ROOT / "configs/pilot_bt_d21_pool_first_24h.yaml"

if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from feature_at_intent_v1 import build_features_at_intent  # noqa: E402
from gate_policies_v1 import evaluate_policies  # noqa: E402
from intent_timeline_v1 import build_intent_timeline, MANDATORY_INTENTS  # noqa: E402
from rules_inventory_v1 import RULES_INVENTORY_MD  # noqa: E402
from sl_pause_replay_v1 import replay_pause_impact, sl_events  # noqa: E402


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
                for k, v in r["ablation"].items():
                    flat[f"ablation_{k}"] = v
            w.writerow(flat)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "rules_r1_r6_inventory.md").write_text(RULES_INVENTORY_MD, encoding="utf-8")

    from input_verify_v1 import verify_inputs  # noqa: E402

    provenance = verify_inputs()
    (OUT / "source_provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    if not provenance.get("all_ok"):
        REPORT.write_text("# BT-D62\n\n**VERDICT:** `CAUSAL_INTENT_GATE_REPLAY_BLOCKED`\n", encoding="utf-8")
        return 2

    from config_yaml_v1 import load_yaml_and_validate  # noqa: E402

    config = load_yaml_and_validate(PILOT)
    intents = build_intent_timeline()
    feature_rows = build_features_at_intent(intents, config)

    sls = sl_events()
    sl_times = sorted(s.get("decision_close_utc") or "" for s in sls)

    gate_rows: list[dict[str, Any]] = []
    precedence_rows: list[dict[str, Any]] = []
    for intent, feat in zip(intents, feature_rows):
        ts = intent["decision_close_utc"]
        paused = any(sl < ts for sl in sl_times if sl)
        pol = evaluate_policies(feat, short_paused=paused)
        d46_outcome = "WOULD_CREATE_PENDING"
        if intent.get("d46_event") != "PENDING_CREATE":
            d46_outcome = "REFERENCE_ONLY"
        elif paused:
            d46_outcome = "SUPPRESSED_BY_PAUSE_RESEARCH"
        elif pol["policy_p2_r6_veto"] == "SHORT_NO":
            d46_outcome = "RESEARCH_GATE_BLOCK"
        elif pol["policy_p2_r6_veto"] == "UNCERTAIN":
            d46_outcome = "RESEARCH_UNCERTAIN"

        gate_rows.append(
            {
                **{k: feat.get(k) for k in (
                    "intent_id",
                    "decision_close_utc",
                    "source",
                    "evidence_class",
                    "ema_validity",
                    "pool_validity",
                    "research_regime",
                    "R1",
                    "R2",
                    "R3",
                    "R4",
                    "R5",
                    "R6_sideways",
                    "first_variant_drop",
                    "gap_to_next_upper_pct",
                    "local_stack_density_0p35pct",
                    "chop_ratio_24b",
                    "net_move_24b_pct",
                ) if k in feat or k in intent},
                **intent,
                **pol,
                "short_paused_at_intent": paused,
                "baseline_d46_pending_contract": d46_outcome,
                "isolated_pool_quality_note": (
                    "gap/stack from depth2 snapshot"
                    if feat.get("pool_validity") in ("OK", "OK_D59_CACHE")
                    else "POOL_NOT_VERIFIED"
                ),
            }
        )
        precedence_rows.append(
            {
                "decision_close_utc": ts,
                "intent_id": intent.get("intent_id"),
                "R5_pass": feat.get("R5"),
                "R6_sideways": feat.get("R6_sideways"),
                "policy_p1_variant_e": pol["policy_p1_variant_e"],
                "policy_p2_r6_veto": pol["policy_p2_r6_veto"],
                "policy_p3": pol["policy_p3_r3_not_r6_plus_isolation"],
                "conflict_variant_e_pass_r6_block": bool(feat.get("R5") and feat.get("R6_sideways")),
            }
        )

    pause_impact, suppressed = replay_pause_impact(intents, gate_rows)

    episodes: list[dict[str, Any]] = []
    for cls, group in Counter(i["evidence_class"] for i in intents).items():
        sub = [g for g in gate_rows if g.get("evidence_class") == cls]
        episodes.append(
            {
                "evidence_class": cls,
                "intent_count": len(sub),
                "independent_episode_estimate": max(1, len({g["decision_close_utc"][:13] for g in sub}) // 3),
                "short_ok_p2": sum(1 for g in sub if g.get("policy_p2_r6_veto") == "SHORT_OK"),
                "short_no_p2": sum(1 for g in sub if g.get("policy_p2_r6_veto") == "SHORT_NO"),
                "uncertain_p2": sum(1 for g in sub if g.get("policy_p2_r6_veto") == "UNCERTAIN"),
            }
        )

    _write_csv(OUT / "baseline_intent_timeline.csv", intents)
    _write_csv(OUT / "gate_decisions_at_intent.csv", gate_rows)
    _write_csv(OUT / "warning_precedence_comparison.csv", precedence_rows)
    _write_csv(OUT / "suppressed_intents.csv", suppressed)
    _write_csv(OUT / "independent_episode_summary.csv", episodes)
    _write_csv(OUT / "sl_pause_impact.csv", pause_impact)

    def _gate(ts: str) -> dict[str, Any]:
        return next(g for g in gate_rows if g["decision_close_utc"] == ts)

    sep9_ok = all(_gate(t)["policy_p2_r6_veto"] == "SHORT_OK" for t in ("2026-09-09T16:30:00Z", "2026-09-09T20:55:00Z"))
    sep11_block = all(
        _gate(t)["policy_p2_r6_veto"] in ("SHORT_NO", "UNCERTAIN")
        for t in ("2026-09-11T12:05:00Z", "2026-09-11T12:30:00Z")
    )
    verdict = "CAUSAL_INTENT_GATE_REPLAY_PASS" if sep9_ok and sep11_block else "CAUSAL_INTENT_GATE_REPLAY_INCONCLUSIVE"

    impl = f"""# BT-D62 implementation recommendation (research only)

- **Do not** wire into `regime_classifier_v1` / production gates yet.
- Provisional gate for isolated tests: **Policy P2** (R5 pass AND NOT R6 sideways; UNCERTAIN if pool missing for R4/R5 path).
- Sep-9: Variant **E** passes at intent; P2 allows if R6 not active.
- Sep-11 12:05/12:30: **R6 SIDEWAYS** from 12:00 blocks under P2 even when E fails earlier.
- SHORT_PAUSED: after first D46 SL ({sl_times[0] if sl_times else 'n/a'}), subsequent D46 intents suppressed in replay.
- Still unvalidated: threshold stability, Sep-9 `UNCERTAIN` regime label, no OOS, D46 pilot window only.
"""
    (OUT / "implementation_recommendation.md").write_text(impl, encoding="utf-8")

    REPORT.write_text(
        "\n".join(
            [
                "# BT-D62 — Causal Short Intent Gate Replay V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## Mandatory intents (decision-close = D41 intent bar)",
                f"- Sep-9 16:30 / 20:55: P2 = `{_gate('2026-09-09T16:30:00Z')['policy_p2_r6_veto']}` / `{_gate('2026-09-09T20:55:00Z')['policy_p2_r6_veto']}`",
                f"- Sep-11 12:05 / 12:30: P2 = `{_gate('2026-09-11T12:05:00Z')['policy_p2_r6_veto']}` / `{_gate('2026-09-11T12:30:00Z')['policy_p2_r6_veto']}` (not fill bars 12:25/12:35)",
                "",
                "## Answers",
                f"1. Both Sep-9 desired allowed (P2)? **{sep9_ok}** (R5 pass; R6 not sideways at anchors).",
                f"2. Sep-11 intents blocked before pending? **{sep11_block}** (R6 veto from 12:00; R5 fails at 12:05).",
                "3. Combination: **R5 + NOT R6** (P2); R1 alone insufficient; R2 false-blocks Sep-9.",
                f"4. D46 pending intents replayed: **{sum(1 for i in intents if i.get('d46_event')=='PENDING_CREATE')}**; independent episode estimate in CSV (not OOS).",
                "5. Provisional research gate justified for **isolated further tests only** — not production.",
                "6. Unvalidated: thresholds, pause requalification, Sep-9 regime UNCERTAIN vs PASS, single pilot day.",
                "",
                "## Safety",
                "No changes to strategy modules, D46, density SSOT, or dashboard.",
                "",
                f"Outputs: `{OUT.relative_to(ROOT)}/`",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "intents": len(intents)}, indent=2))
    return 0 if verdict != "CAUSAL_INTENT_GATE_REPLAY_BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
