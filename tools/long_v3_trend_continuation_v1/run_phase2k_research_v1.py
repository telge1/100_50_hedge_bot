#!/usr/bin/env python3
"""LONG V3 Phase 2K — frozen (A) vs dynamic upper (B) buy limit comparison."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2k_dynamic_buy_v1"
REPORT = ROOT / "reports/LONG_V3_PHASE2K_DYNAMIC_BUY_LIMIT_COMPARISON_V1.md"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"
YAML = ROOT / "configs/long_v3_trend_continuation_research_v1.yaml"

WINDOWS = {
    "primary_16_17": ("2026-09-16T00:00:00Z", "2026-09-17T23:55:00Z"),
    "control_20_21": ("2026-09-20T00:00:00Z", "2026-09-21T23:55:00Z"),
    "independent_18_19": ("2026-09-18T00:00:00Z", "2026-09-19T23:55:00Z"),
}

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


def _baseline(yaml_sup, max_risk, buy_sem: str) -> LongResearchConfigV2DV1:
    return LongResearchConfigV2DV1(
        support_mode="V2E_QUALIFIED",
        sl_mode="DEEPER_Q_UPPER",
        recovery_mode="NEXT_BAR",
        v2e_yaml=yaml_sup,
        max_risk_pct=max_risk,
        pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
        formation_semantics="CAUSAL_BELOW_PRICE_SEGMENT",
        touch_lifecycle="LINEAGE_AWARE_V2J",
        buy_limit_semantics=buy_sem,  # type: ignore[arg-type]
    )


def _trade_row(t) -> dict:
    tp_gap = None
    if t.tp_limit is not None and t.entry_price:
        tp_gap = round((t.tp_limit - t.entry_price) / t.entry_price * 100.0, 8)
    return {
        "signal_close_utc": t.signal_close_utc,
        "fill_bar_utc": t.fill_bar_utc,
        "entry_price": t.entry_price,
        "sl_price": t.sl_price,
        "tp_limit": t.tp_limit,
        "risk_pct": t.risk_pct,
        "tp_gap_pct": tp_gap,
        "exit_reason": t.exit_reason,
        "gross_return_pct": t.gross_return_pct,
        "pool_key": t.pool_key,
    }


def _run_window(session, closes, prices, emas, i0, i1, cfg) -> dict:
    r = run_long_research_v2d(
        bars=closes,
        prices=prices,
        ema9=emas[9],
        ema20=emas[20],
        ema59=emas[59],
        ema200=emas[200],
        session=session,
        entry_config=cfg,
        exit_variant="LONG_A",
        eval_start_index=i0,
        eval_end_index=i1,
    )
    fills = [_trade_row(t) for t in r.trades]
    fill_keys = {f"{x['fill_bar_utc']}|{x['entry_price']}" for x in fills}
    return {
        "summary": r.summary,
        "fills": fills,
        "fill_keys": sorted(fill_keys),
        "repricing_timeline": r.repricing_audits,
        "entry_events_repricing": [
            e for e in r.entry_events if e.get("action") in ("REPLACE", "REPRICE_BLOCKED", "CANCEL")
            and "DYNAMIC" in str(e.get("reason", "")) + str(e.get("replace_kind", ""))
        ],
        "touch_event_counts": r.summary.get("touch_event_counts", {}),
    }


def _diff_fills(a: dict, b: dict) -> dict:
    set_a = set(a["fill_keys"])
    set_b = set(b["fill_keys"])
    return {
        "only_in_A": sorted(set_a - set_b),
        "only_in_B": sorted(set_b - set_a),
        "shared": sorted(set_a & set_b),
    }


def _write_report(data: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    a = data["answers"]
    lines = [
        "# LONG V3 Phase 2K — Dynamic Buy Limit (Variant B)",
        "",
        f"**Verdict:** `{data['verdict']}`",
        "",
        "## 1. Technische Umsetzung Variante B",
        "",
        a["1_implementation"],
        "",
        "## 2. Variante A unverändert",
        "",
        a["2_variant_a_unchanged"],
        "",
        "## 3. Historische Fenster",
        "",
        a["3_windows"],
        "",
        "## 4. Order-Repricing-Timelines (Variante B)",
        "",
        "```json",
        json.dumps(data["all_repricing"], indent=2)[:15000],
        "```",
        "",
        "## 5. Zusätzliche / verpasste Fills",
        "",
        a["5_fill_diff"],
        "",
        "## 6. Risiko- und TP-Abstände",
        "",
        a["6_risk_tp"],
        "",
        "## 7. Regressionstests",
        "",
        a["7_tests"],
        "",
        "## 8. Empfehlung",
        "",
        a["8_recommendation"],
        "",
        "## Metriken je Fenster",
        "",
        "```json",
        json.dumps(data["window_metrics"], indent=2),
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

    cfg_a = _baseline(ly.support, ly.max_risk_pct, "FROZEN_AT_PLACE_V2J")
    cfg_b = _baseline(ly.support, ly.max_risk_pct, "DYNAMIC_UPPER_GROWTH_V2K")

    window_metrics: dict[str, dict] = {}
    all_repricing: dict[str, list] = {}
    fill_diffs: dict[str, dict] = {}

    for wname, (w0, w1) in WINDOWS.items():
        i0, i1 = closes.index(w0), closes.index(w1)
        gate = evaluate_pool_history_gate(close_index=i0)
        if not gate.ok:
            window_metrics[wname] = {"blocked": True, "reason": gate.reason_code}
            continue
        ra = _run_window(session, closes, prices, emas, i0, i1, cfg_a)
        rb = _run_window(session, closes, prices, emas, i0, i1, cfg_b)
        diff = _diff_fills(ra, rb)
        fill_diffs[wname] = diff
        all_repricing[wname] = rb["repricing_timeline"]
        window_metrics[wname] = {
            "VARIANT_A_FROZEN": ra["summary"],
            "VARIANT_B_DYNAMIC": rb["summary"],
            "fill_diff": diff,
            "repricing_count_B": len(rb["repricing_timeline"]),
        }

    long_test = subprocess.run(
        [sys.executable, "-m", "pytest", str(PKG / "tests"), "-q", "--tb=no"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    short_test = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tools/phase_short_main_v2_combined_exit_v1/tests"), "-q", "--tb=no"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )

    prim = window_metrics.get("primary_16_17", {})
    sa = prim.get("VARIANT_A_FROZEN", {})
    sb = prim.get("VARIANT_B_DYNAMIC", {})
    extra_b = len(fill_diffs.get("primary_16_17", {}).get("only_in_B", []))
    missed_b = len(fill_diffs.get("primary_16_17", {}).get("only_in_A", []))

    if long_test.returncode != 0:
        verdict = "PHASE2K_BLOCKED"
    elif sb.get("fills", 0) > sa.get("fills", 0) or extra_b > 0:
        verdict = "PHASE2K_DYNAMIC_MEASURABLE_DELTA"
    elif sb.get("fills", 0) == sa.get("fills", 0) and sb.get("sum_gross_return_pct") == sa.get("sum_gross_return_pct"):
        verdict = "PHASE2K_NO_MEASURABLE_ADVANTAGE"
    else:
        verdict = "PHASE2K_MIXED"

    report_data = {
        "verdict": verdict,
        "ohlc_meta": ohlc_meta,
        "window_metrics": window_metrics,
        "all_repricing": all_repricing,
        "fill_diffs": fill_diffs,
        "configs": {"A": cfg_a.label(), "B": cfg_b.label()},
        "answers": {
            "1_implementation": (
                "`buy_limit_semantics=DYNAMIC_UPPER_GROWTH_V2K`: Nach `_try_fill_pending` und Pending-Validierung "
                "prüft `try_dynamic_upper_growth_reprice` dieselbe Lineage; bei strikt höherer Oberkante → "
                "Re-Qualifikation (Formation/Gap/EMA), LONG-A-TP ≥0,5 %, Risiko; dann CANCEL+PLACE+REPLACE "
                "mit neuem Limit an `P.upper_edge`, earliest next open. Oberkante unverändert / nur Lower-Wachstum: "
                "kein Repricing. Limit ≥ Markt: fail-closed Audit."
            ),
            "2_variant_a_unchanged": (
                "Default `FROZEN_AT_PLACE_V2J` — identischer Codepfad wie Phase 2J; Union-Drift nur "
                "`PENDING_FROZEN_PRICES_HELD`. Keine Änderung an Touch/Recovery/PLACE für A."
            ),
            "3_windows": "\n".join(
                f"- **{k}**: A fills={window_metrics[k]['VARIANT_A_FROZEN']['fills']}, "
                f"B fills={window_metrics[k]['VARIANT_B_DYNAMIC']['fills']}, "
                f"repricing B={window_metrics[k]['repricing_count_B']}"
                for k in WINDOWS
                if k in window_metrics and not window_metrics[k].get("blocked")
            ),
            "5_fill_diff": json.dumps(fill_diffs, indent=2),
            "6_risk_tp": (
                "Pro Trade: `risk_pct` und `tp_gap_pct` aus Runner-Feldern (LONG A TP an Fill). "
                "Repricing: `entry_change_pct` / `tp_gap_pct` in `repricing_audits`."
            ),
            "7_tests": (
                f"Long pytest rc={long_test.returncode}. Short rc={short_test.returncode} "
                "(June D107 weiter BLOCKED wenn Bundle fehlt)."
            ),
            "8_recommendation": (
                f"Primary 16–17: A {sa.get('fills', '?')} Fills / {sa.get('sum_gross_return_pct', '?')} % Brutto-Summe; "
                f"B {sb.get('fills', '?')} Fills / {sb.get('sum_gross_return_pct', '?')} %; "
                f"+{extra_b} nur in B, −{missed_b} nur in A. "
                "Keine Profitabilitätsaussage — Forschungsbaseline bleibt A (NEXT_BAR frozen), "
                "B nur wenn Nachziehen kausal mehr gültige Setups liefert."
            ),
        },
    }
    (OUT / "phase2k_report.json").write_text(json.dumps(report_data, indent=2, default=str), encoding="utf-8")
    _write_report(report_data)
    print(verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
