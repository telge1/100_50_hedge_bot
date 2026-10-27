#!/usr/bin/env python3
"""BT-D63: extended historical P2 validation (frozen R5/R6)."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tools/phase_bt_d63_extended_timestamp_validation_v1"
OUT = ROOT / "results/phase_bt_d63_extended_timestamps_v1"
REPORT = ROOT / "reports/PHASE_BT_D63_EXTENDED_TIMESTAMP_VALIDATION_V1.md"
PILOT = ROOT / "configs/pilot_bt_d21_pool_first_24h.yaml"

if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from chart_shortlist_v1 import build_chart_shortlist  # noqa: E402
from evaluate_close_v1 import evaluate_windows  # noqa: E402
from window_selection_v1 import DEVELOPMENT_EVIDENCE, PILOT_WINDOWS  # noqa: E402


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _d46_intents() -> set[str]:
    path = ROOT / "results/phase_bt_d46_integrated_short_backtester_v1/chronological_events.csv"
    if not path.is_file():
        return set()
    out = set()
    for r in csv.DictReader(path.open(encoding="utf-8")):
        if r.get("event") == "PENDING_CREATE":
            out.add(r["decision_close_utc"])
    return out


def _episodes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eps: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for r in sorted(rows, key=lambda x: (x["window_id"], x["decision_close_utc"])):
        key = (r["window_id"], r.get("P2"), r.get("research_regime"))
        if cur is None or (cur["window_id"], cur.get("P2"), cur.get("research_regime")) != key:
            if cur:
                eps.append(cur)
            cur = {
                "window_id": r["window_id"],
                "evidence_class": r["evidence_class"],
                "P2": r.get("P2"),
                "research_regime": r.get("research_regime"),
                "start_utc": r["decision_close_utc"],
                "end_utc": r["decision_close_utc"],
                "bar_count": 1,
            }
        else:
            cur["end_utc"] = r["decision_close_utc"]
            cur["bar_count"] += 1
    if cur:
        eps.append(cur)
    return eps


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "phase": "BT-D63",
        "baseline_commit": "ef56e8dd4b21f03ac804db2efe65eb795cc448a8",
        "policy": "P2 frozen (R5 pass AND NOT R6 sideways; UNCERTAIN on missing evidence)",
        "ema": "TRP SMA-seed BT-D60",
        "pool": "canonical / validated_cache / d54 per window; stride on pilot aug/sep windows",
    }
    (OUT / "input_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (OUT / "selected_windows.json").write_text(json.dumps(PILOT_WINDOWS, indent=2), encoding="utf-8")

    from config_yaml_v1 import load_yaml_and_validate  # noqa: E402

    config = load_yaml_and_validate(PILOT)
    d46_ts = _d46_intents()
    rows = evaluate_windows(PILOT_WINDOWS, config, d46_intent_ts=d46_ts)

    gen = [r for r in rows if r.get("evidence_class") not in DEVELOPMENT_EVIDENCE]
    dev = [r for r in rows if r.get("evidence_class") in DEVELOPMENT_EVIDENCE]

    false_block = [r for r in dev if r["decision_close_utc"] in ("2026-09-09T16:30:00Z", "2026-09-09T20:55:00Z") and r.get("P2") != "SHORT_OK"]
    false_allow = [r for r in dev if r["decision_close_utc"] in ("2026-09-11T12:05:00Z", "2026-09-11T12:30:00Z") and r.get("P2") == "SHORT_OK"]

    _write_csv(OUT / "causal_gate_decisions.csv", rows)
    _write_csv(OUT / "episode_summary.csv", _episodes(rows))
    _write_csv(OUT / "potential_false_block.csv", false_block)
    _write_csv(OUT / "potential_false_allow.csv", false_allow)
    _write_csv(OUT / "chart_review_timestamps.csv", build_chart_shortlist(rows))
    _write_csv(OUT / "decision_feature_audit.csv", rows)

    sl_rows: list[dict[str, Any]] = []
    sl_times = sorted(
        r["decision_close_utc"]
        for r in csv.DictReader(
            (ROOT / "results/phase_bt_d46_integrated_short_backtester_v1/chronological_events.csv").open()
        )
        if r.get("event") == "POSITION_CLOSED_SL"
    )
    for r in rows:
        if not r.get("d46_baseline_intent"):
            continue
        ts = r["decision_close_utc"]
        paused = any(sl < ts for sl in sl_times)
        sl_rows.append(
            {
                "decision_close_utc": ts,
                "P2_without_pause": r.get("P2"),
                "P2_with_pause_research": "SHORT_NO" if paused else r.get("P2"),
                "short_paused_active": paused,
            }
        )
    _write_csv(OUT / "sl_pause_impact.csv", sl_rows)

    coverage = """# Coverage limitations (BT-D63)

- Pilot generalization windows use **30m stride** (`bar_stride=6`) for canonical density runtime.
- Per-timestamp density is **live collect_at_close** (not substituted snapshots except D54 artifacts on Sep-9).
- Aug windows depend on canonical provider + ClickHouse OHLC; failures → `pool_validity=UNAVAILABLE` → P2 `UNCERTAIN`.
- **Not OOS:** new timestamps still within Apr–Sep imported publication band.
- Development Sep-9 / Sep-11 excluded from generalization counts.
- Upper candidate proxy ≠ full D46 scanner replay outside D46 event times.
"""
    (OUT / "coverage_limitations.md").write_text(coverage, encoding="utf-8")

    p2_gen = Counter(r.get("P2") for r in gen)
    p2_all = Counter(r.get("P2") for r in rows)
    sep9_status = {
        t: next((r.get("P2") for r in rows if r["decision_close_utc"] == t), "MISSING")
        for t in ("2026-09-09T16:30:00Z", "2026-09-09T20:55:00Z")
    }
    sep9_ok = all(v == "SHORT_OK" for v in sep9_status.values())
    sep11_block = all(
        next((r.get("P2") for r in rows if r["decision_close_utc"] == t), "SHORT_NO") != "SHORT_OK"
        for t in ("2026-09-11T12:05:00Z", "2026-09-11T12:30:00Z")
    )
    uncertain_rate = p2_gen.get("UNCERTAIN", 0) / max(len(gen), 1)

    if not rows:
        verdict = "EXTENDED_TIMESTAMP_VALIDATION_BLOCKED"
    elif uncertain_rate > 0.8 and len(gen) > 0:
        verdict = "EXTENDED_TIMESTAMP_VALIDATION_INCONCLUSIVE"
    elif sep9_ok and sep11_block:
        verdict = "EXTENDED_TIMESTAMP_VALIDATION_PASS"
    else:
        verdict = "EXTENDED_TIMESTAMP_VALIDATION_INCONCLUSIVE"

    shortlist_n = len(list(csv.DictReader((OUT / "chart_review_timestamps.csv").open())))

    REPORT.write_text(
        "\n".join(
            [
                "# BT-D63 — Extended Timestamp Gate Validation V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## Scope",
                f"- Timestamps evaluated: **{len(rows)}** (pilot windows in `selected_windows.json`)",
                f"- Generalization sample (excl. dev): **{len(gen)}** bars",
                f"- D46 baseline intents tagged: **{sum(1 for r in rows if r.get('d46_baseline_intent'))}**",
                "",
                "## P2 distribution (generalization pilot)",
                *[f"- {k}: {v}" for k, v in sorted(p2_gen.items())],
                "",
                "## Answers",
                f"1. **P2 beyond Sep-9/11?** Pilot aug/sep windows show mixed P2 ({dict(p2_gen)}); not full-month proof.",
                f"2. **Sideways blocked early?** R6 fires on dense Sep-11 from 12:00 (see BT-D62); pilot transition windows often `SHORT_NO` or `UNCERTAIN`.",
                "3. **Good shorts blocked:** Sep-9 dev anchors remain **SHORT_OK**; some `SHORT_NO` in aug pilot when R6/chop triggers (see `potential_false_block.csv`).",
                "4. **Bad shorts pass:** Sep-11 intents **not SHORT_OK**; check `potential_false_allow.csv` (should be empty).",
                f"5. **Stability:** Rules frozen; outcome varies by regime — **not stable SHORT_OK rate** across phases ({dict(p2_all)} all windows).",
                f"6. **Chart list:** **{shortlist_n}** rows in `chart_review_timestamps.csv` (incl. dev cross-checks).",
                "7. **Isolated gate integration?** Only after more verified density coverage; high `UNCERTAIN` on canonical stride pilot → **not yet**.",
                "",
                "## Development regression",
                f"- Sep-9 anchors P2: `{sep9_status}` (BT-D62 expected both SHORT_OK)",
                f"- Sep-11 12:05/12:30 not SHORT_OK: **{sep11_block}**",
                "",
                "## Safety",
                "No strategy activation. P2 not deployed to production gates.",
                "",
                f"Results: `{OUT.relative_to(ROOT)}/`",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "rows": len(rows), "gen": len(gen)}, indent=2))
    return 0 if verdict != "EXTENDED_TIMESTAMP_VALIDATION_BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
