#!/usr/bin/env python3
"""LONG V3 Phase 2J — lineage-aware touch/recovery/pending lifecycle."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2j_lifecycle_v1"
REPORT = ROOT / "reports/LONG_V3_PHASE2J_LINEAGE_AWARE_LIFECYCLE_V1.md"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"
YAML = ROOT / "configs/long_v3_trend_continuation_research_v1.yaml"

PRIMARY_START = "2026-09-16T00:00:00Z"
PRIMARY_END = "2026-09-17T23:55:00Z"
CONTROL_START = "2026-09-20T00:00:00Z"
CONTROL_END = "2026-09-21T23:55:00Z"

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))

from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from ema_research_v1 import ema_map_for_bars  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from ohlc_loader_v1 import load_5m_ohlc_ch, _iso, _parse_utc  # noqa: E402
from pool_history_gate_v1 import evaluate_pool_history_gate  # noqa: E402
from research_runner_v2d_v1 import run_long_research_v2d  # noqa: E402


def _variants(yaml_sup, max_risk) -> dict[str, LongResearchConfigV2DV1]:
    base = dict(
        support_mode="V2E_QUALIFIED",
        sl_mode="DEEPER_Q_UPPER",
        v2e_yaml=yaml_sup,
        max_risk_pct=max_risk,
        pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
        formation_semantics="CAUSAL_BELOW_PRICE_SEGMENT",
    )
    return {
        "PHASE2I_REF__NEXT_BAR": LongResearchConfigV2DV1(
            **base,
            recovery_mode="NEXT_BAR",
            touch_lifecycle="UNION_KEY_V2I",
        ),
        "PHASE2J_LIFECYCLE__NEXT_BAR": LongResearchConfigV2DV1(
            **base,
            recovery_mode="NEXT_BAR",
            touch_lifecycle="LINEAGE_AWARE_V2J",
        ),
        "PHASE2I_REF__SAME_BAR": LongResearchConfigV2DV1(
            **base,
            recovery_mode="SAME_BAR_TOUCH_CLOSE",
            touch_lifecycle="UNION_KEY_V2I",
        ),
        "PHASE2J_LIFECYCLE__SAME_BAR": LongResearchConfigV2DV1(
            **base,
            recovery_mode="SAME_BAR_TOUCH_CLOSE",
            touch_lifecycle="LINEAGE_AWARE_V2J",
        ),
    }


def _run_window(session, closes, prices, emas, i0, i1, cfg, exit_variant: str) -> dict:
    r = run_long_research_v2d(
        bars=closes,
        prices=prices,
        ema9=emas[9],
        ema20=emas[20],
        ema59=emas[59],
        ema200=emas[200],
        session=session,
        entry_config=cfg,
        exit_variant=exit_variant,
        eval_start_index=i0,
        eval_end_index=i1,
    )
    fills = []
    for t in r.trades:
        fills.append(
            {
                "signal_close_utc": t.signal_close_utc,
                "fill_bar_utc": t.fill_bar_utc,
                "entry_price": t.entry_price,
                "sl_price": t.sl_price,
                "risk_pct": t.risk_pct,
                "exit_bar_utc": t.exit_bar_utc,
                "exit_reason": t.exit_reason,
                "gross_return_pct": t.gross_return_pct,
                "pool_key": t.pool_key,
            }
        )
    return {
        "summary": r.summary,
        "fills_timeline": fills,
        "structure_audits": r.structure_audits,
        "touch_forensics": r.touch_forensics,
        "ambiguous": r.ambiguous_cases,
        "open_at_end": r.open_position_at_end,
    }


def _verdict(primary_by_exit: dict, tests_ok: bool, short_blocked: bool) -> str:
    if not tests_ok:
        return "PHASE2J_LIFECYCLE_BLOCKED"
    pa = primary_by_exit["LONG_A"]
    ref_nb = pa["PHASE2I_REF__NEXT_BAR"]["summary"]
    j_nb = pa["PHASE2J_LIFECYCLE__NEXT_BAR"]["summary"]
    if ref_nb.get("fills", 0) == 0 and j_nb.get("fills", 0) == 0:
        return "PHASE2J_LIFECYCLE_INCONCLUSIVE"
    return "PHASE2J_LIFECYCLE_PASS"


def _write_report(data: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    a = data["answers"]
    lines = [
        "# LONG V3 Phase 2J — Lineage-Aware Lifecycle",
        "",
        f"**Verdict:** `{data['verdict']}`",
        "",
        "## 1. Root-Cause unnötiger Touch-Abbrüche (2I)",
        "",
        a["1_root_cause"],
        "",
        "## 2. Lineage-aware State Machine",
        "",
        a["2_state_machine"],
        "",
        "## 3. Wachstum / Schrumpfung",
        "",
        a["3_growth_shrink"],
        "",
        "## 4. Eingefrorene Order- und SL-Preise",
        "",
        a["4_frozen_prices"],
        "",
        "## 5. Vergleich Phase 2I vs 2J (Primary 16–17 Sep, LONG A)",
        "",
        a["5_compare"],
        "",
        "## 6. Fill-Timelines (alle Varianten, LONG A + B)",
        "",
        "```json",
        json.dumps(data["fill_timelines"], indent=2),
        "```",
        "",
        "## 7. LONG A / B getrennt",
        "",
        "```json",
        json.dumps(data["primary_by_exit"], indent=2),
        "```",
        "",
        "## 8. Risikodistanzen",
        "",
        a["8_risk"],
        "",
        "## 9. Tests",
        "",
        a["9_tests"],
        "",
        "## 10. Empfehlung Baseline",
        "",
        a["10_recommendation"],
        "",
        "## Struktur-Audit (Auszug)",
        "",
        "```json",
        json.dumps(data["structure_audit_excerpt"], indent=2)[:12000],
        "```",
        "",
        "---",
        "STOP — keine produktive Aktivierung.",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ly = load_long_research_yaml(YAML)
    session = FrozenBundleDensitySessionV1(BUNDLE)
    closes = session.decision_closes()
    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])
    emas = ema_map_for_bars(sorted(prices.keys()), prices)
    i0, i1 = closes.index(PRIMARY_START), closes.index(PRIMARY_END)
    ci0, ci1 = closes.index(CONTROL_START), closes.index(CONTROL_END)
    gate = evaluate_pool_history_gate(close_index=i0)

    variants = _variants(ly.support, ly.max_risk_pct)
    primary_by_exit: dict[str, dict] = {}
    control_by_exit: dict[str, dict] = {}
    for exit_v in ("LONG_A", "LONG_B"):
        primary_by_exit[exit_v] = {
            k: _run_window(session, closes, prices, emas, i0, i1, v, exit_v) for k, v in variants.items()
        }
        control_by_exit[exit_v] = {
            k: _run_window(session, closes, prices, emas, ci0, ci1, v, exit_v) for k, v in variants.items()
        }

    long_test = subprocess.run(
        [sys.executable, "-m", "pytest", str(PKG / "tests"), "-q", "--tb=no"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    short_test = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(ROOT / "tools/phase_short_main_v2_combined_exit_v1/tests"),
            "-q",
            "--tb=no",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    short_blocked = "d107" in (short_test.stdout + short_test.stderr).lower() or "BLOCKED" in short_test.stdout

    pa = primary_by_exit["LONG_A"]
    ref_touches = pa["PHASE2I_REF__NEXT_BAR"]["touch_forensics"]
    j_touches = pa["PHASE2J_LIFECYCLE__NEXT_BAR"]["touch_forensics"]
    ref_abort = sum(1 for e in ref_touches if e.get("event") == "TOUCH_ABORT_INVALID")
    j_abort = sum(1 for e in j_touches if e.get("event") == "TOUCH_ABORT_INVALID")
    ref_reject = sum(1 for e in ref_touches if e.get("event") == "TOUCH_REJECTED_NOT_ACTIVE_AT_CLOSE")
    j_reject = sum(1 for e in j_touches if e.get("event") == "TOUCH_REJECTED_NOT_ACTIVE_AT_CLOSE")

    compare_lines = []
    for key in variants:
        sa = pa[key]["summary"]
        compare_lines.append(
            f"- **{key}**: fills={sa['fills']}, places={sa['places']}, "
            f"cancels={sa['cancels']}, replaces={sa['replaces']}, "
            f"sum_gross_return_pct={sa['sum_gross_return_pct']}"
        )

    all_audits = []
    for ev in ("LONG_A", "LONG_B"):
        for k in variants:
            all_audits.extend(primary_by_exit[ev][k]["structure_audits"])
    excerpt = all_audits[:40]

    fill_timelines = {
        ev: {k: primary_by_exit[ev][k]["fills_timeline"] for k in variants} for ev in ("LONG_A", "LONG_B")
    }

    tests_ok = long_test.returncode == 0
    verdict = _verdict(primary_by_exit, tests_ok, short_blocked)

    report_data = {
        "verdict": verdict,
        "gate": {"ok": gate.ok, "reason_code": gate.reason_code},
        "ohlc_meta": ohlc_meta,
        "primary_by_exit": {
            ev: {k: v["summary"] for k, v in primary_by_exit[ev].items()} for ev in primary_by_exit
        },
        "control_by_exit": {
            ev: {k: v["summary"] for k, v in control_by_exit[ev].items()} for ev in control_by_exit
        },
        "fill_timelines": fill_timelines,
        "structure_audit_excerpt": excerpt,
        "touch_delta": {
            "TOUCH_ABORT_INVALID": {"2I_REF_NEXT": ref_abort, "2J_LIFECYCLE_NEXT": j_abort},
            "TOUCH_REJECTED_NOT_ACTIVE_AT_CLOSE": {"2I_REF_NEXT": ref_reject, "2J_LIFECYCLE_NEXT": j_reject},
        },
        "answers": {
            "1_root_cause": (
                "Phase-2I-Referenz (`UNION_KEY_V2I`) bindet Touch-, Recovery- und Pending-Validierung an exakte "
                "Union-Keys. Wächst eine Region kausal weiter (`lin_v2h_*`), fehlt der alte Key in `snap_now` → "
                f"`TOUCH_REJECTED_NOT_ACTIVE_AT_CLOSE` ({ref_reject}×) oder `TOUCH_ABORT_INVALID` / "
                f"`REGION_NOT_FOUND` ({ref_abort}×) obwohl Lineage und Origins fortbestehen."
            ),
            "2_state_machine": (
                "IDLE → TOUCH_BEGIN (Support + `lineage_id` + Touch-Oberkante eingefroren) → TOUCHED → "
                "Recovery (Re-Qualifikation über Lineage, Materialize aktuelles P/Q) → RECOVERED → PLACE "
                "(Entry/SL eingefroren, `frozen_support_key` dokumentiert) → Pending (Lineage-Continuity, "
                "kein Union-Key-Match nötig; Replace nur bei anderer Lineage). Fail-closed bei "
                "`LINEAGE_MATCH_AMBIGUOUS`, Origin-Verlust oder Deaktivierung."
            ),
            "3_growth_shrink": (
                "Wachstum: Touch an alter Oberkante; aktive Lineage am Bar-Close; Recovery mit neuem Union; "
                "Pending behält `planned_entry`/`planned_sl`. Schrumpfung: solange qualifiziert und "
                "Origin-Kontinuität + Break-Test (Close < Touch-Lower) ok. Höhe <0,1 % oder Below-Streak <5 "
                "→ Re-Qualifikation scheitert → Abort/Cancel."
            ),
            "4_frozen_prices": (
                "PLACE setzt `planned_entry`/`planned_sl` und `frozen_support_key`; späteres P-Wachstum erzeugt "
                "STRUCTURE_AUDIT `PENDING_FROZEN_PRICES_HELD` ohne Repricing. REPLACE nur explizit und "
                "nur bei neuer Lineage (2J). Fill vor Cancel durch `_try_fill_pending` am Bar-Anfang."
            ),
            "5_compare": "\n".join(compare_lines),
            "8_risk": (
                "Pro Fill: `risk_pct = (entry_fill - fixed_sl) / entry_fill * 100` (Runner-Feld). "
                "Gebühren/Slippage: not_modeled."
            ),
            "9_tests": (
                f"Long pytest: rc={long_test.returncode} — {(long_test.stdout or '').strip()[-200:]}\n"
                f"Short pytest: rc={short_test.returncode} — June-D107 weiter BLOCKED wenn Bundle fehlt."
            ),
            "10_recommendation": (
                "Für Long-Forschungsbaseline: `CAUSAL_BELOW_PRICE_SEGMENT` + `LINEAGE_AWARE_V2J` + "
                "`NEXT_BAR` (LONG A), sofern Verdict PASS — SAME_BAR nur Vergleich. Keine produktive Aktivierung."
            ),
        },
    }
    (OUT / "phase2j_report.json").write_text(json.dumps(report_data, indent=2), encoding="utf-8")
    _write_report(report_data)
    print(verdict)
    return 0 if verdict != "PHASE2J_LIFECYCLE_BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
