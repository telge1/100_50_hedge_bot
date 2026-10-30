#!/usr/bin/env python3
"""LONG V3 Phase 2L — full A vs B evidence export (read-only replay)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2l_evidence_v1"
REPORT = ROOT / "reports/LONG_V3_PHASE2L_FIXED_VS_DYNAMIC_EVIDENCE_V1.md"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"
YAML = ROOT / "configs/long_v3_trend_continuation_research_v1.yaml"

WINDOWS = {
    "primary_16_17": ("2026-09-16T00:00:00Z", "2026-09-17T23:55:00Z"),
    "independent_18_19": ("2026-09-18T00:00:00Z", "2026-09-19T23:55:00Z"),
    "control_20_21": ("2026-09-20T00:00:00Z", "2026-09-21T23:55:00Z"),
}

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))

from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from ema_research_v1 import ema_map_for_bars  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_phase2l_evidence_v1 import (  # noqa: E402
    audit_b_place_replace_tp,
    build_trade_dossier,
    dossier_to_chart_row,
    fmt_price,
    window_comparison_table,
    write_trade_chart_csv,
)
from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from ohlc_loader_v1 import load_5m_ohlc_ch, _iso, _parse_utc  # noqa: E402
from research_runner_v2d_v1 import run_long_research_v2d  # noqa: E402


def _cfg(yaml_sup, max_risk, buy: str) -> LongResearchConfigV2DV1:
    return LongResearchConfigV2DV1(
        support_mode="V2E_QUALIFIED",
        sl_mode="DEEPER_Q_UPPER",
        recovery_mode="NEXT_BAR",
        v2e_yaml=yaml_sup,
        max_risk_pct=max_risk,
        pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
        formation_semantics="CAUSAL_BELOW_PRICE_SEGMENT",
        touch_lifecycle="LINEAGE_AWARE_V2J",
        buy_limit_semantics=buy,  # type: ignore[arg-type]
    )


def _run(session, closes, prices, emas, i0, i1, cfg):
    return run_long_research_v2d(
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


def _case_study_b_fill(ledger_b, repricing, dossier_b, ledger_a) -> dict:
    """Forensics for 2026-09-17 ~02:15 B fill at 0.08064."""
    target = None
    for d in dossier_b:
        if d["fill_utc"] == "2026-09-17T02:15:00Z" and abs(float(d["entry_price_usdt"]) - 0.08064) < 1e-9:
            target = d
            break
    rep = [r for r in repricing if r.get("bar_utc") == "2026-09-17T02:10:00Z"]
    a_same_window = [
        e
        for e in ledger_a
        if e.get("phase") == "entry"
        and e.get("bar_utc", "") >= "2026-09-17T01:00:00Z"
        and e.get("bar_utc", "") <= "2026-09-17T03:00:00Z"
    ]
    return {
        "trade": target,
        "repricing_02_10": rep,
        "ledger_B_slice": [
            e
            for e in ledger_b
            if e.get("bar_utc", "") >= "2026-09-17T01:30:00Z"
            and e.get("bar_utc", "") <= "2026-09-17T02:20:00Z"
            and e.get("phase") == "entry"
        ],
        "ledger_A_slice": a_same_window,
        "narrative": (
            "B: REPLACE 02:10 UTC hebt Limit 0.08051→0.08064 (Lineage lin_v2h_551); Fill 02:15 @ 0.08064. "
            "A: Limit bleibt 0.08051 (FROZEN) — kein Fill in dieser Sequenz, da Kurs das höhere B-Limit erreicht "
            "ohne Retrace auf 0.08051."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ly = load_long_research_yaml(YAML)
    session = FrozenBundleDensitySessionV1(BUNDLE)
    closes = session.decision_closes()
    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, _ = load_5m_ohlc_ch(ohlc_start, closes[-1])
    emas = ema_map_for_bars(sorted(prices.keys()), prices)

    cfg_a = _cfg(ly.support, ly.max_risk_pct, "FROZEN_AT_PLACE_V2J")
    cfg_b = _cfg(ly.support, ly.max_risk_pct, "DYNAMIC_UPPER_GROWTH_V2K")

    all_dossiers: list[dict] = []
    comparisons: dict[str, dict] = {}
    tp_violations_b: list[dict] = []
    case_study = None

    for wname, (w0, w1) in WINDOWS.items():
        i0, i1 = closes.index(w0), closes.index(w1)
        ra = _run(session, closes, prices, emas, i0, i1, cfg_a)
        rb = _run(session, closes, prices, emas, i0, i1, cfg_b)
        ledger_a = ra.order_event_ledger
        ledger_b = rb.order_event_ledger
        tp_violations_b.extend(audit_b_place_replace_tp(ledger_b, session))

        da = [
            build_trade_dossier(
                variant="A",
                window=wname,
                trade=t,
                ledger=ledger_a,
                repricing_audits=ra.repricing_audits,
                session=session,
                open_at_end=ra.open_position_at_end,
            )
            for t in ra.trades
        ]
        db = [
            build_trade_dossier(
                variant="B",
                window=wname,
                trade=t,
                ledger=ledger_b,
                repricing_audits=rb.repricing_audits,
                session=session,
                open_at_end=rb.open_position_at_end,
            )
            for t in rb.trades
        ]
        all_dossiers.extend(da)
        all_dossiers.extend(db)
        comparisons[wname] = window_comparison_table(da, db, ra.summary, rb.summary)
        if wname == "primary_16_17":
            case_study = _case_study_b_fill(ledger_b, rb.repricing_audits, db, ledger_a)

    write_trade_chart_csv(OUT / "trade_chart_review.csv", all_dossiers)
    json_payload = {
        "windows": WINDOWS,
        "comparisons": comparisons,
        "trades": all_dossiers,
        "case_study_17sep_0215_B": case_study,
        "variant_B_tp_audit_violations": tp_violations_b,
        "chart_review_rows": [dossier_to_chart_row(d) for d in all_dossiers],
    }
    (OUT / "trade_chart_review.json").write_text(json.dumps(json_payload, indent=2), encoding="utf-8")

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
    short_blocked = "FAILED" in short_test.stdout or short_test.returncode != 0

    only_b_primary = comparisons["primary_16_17"]["only_B"]
    b_advantage_single = len(only_b_primary) == 1 and "0.08064" in only_b_primary[0]

    chart_table = [dossier_to_chart_row(d) for d in sorted(all_dossiers, key=lambda x: (x["fill_utc"], x["variant"]))]

    md_lines = [
        "# LONG V3 Phase 2L — Fixed vs Dynamic Buy Limit Evidence",
        "",
        "**STOP — Auswertung only, keine produktive Aktivierung.**",
        "",
        "## 1. A/B-Vergleich ( drei Fenster )",
        "",
        "```json",
        json.dumps(comparisons, indent=2),
        "```",
        "",
        "## 2. Zusätzlicher B-Fill 17.09.",
        "",
        "```json",
        json.dumps(case_study, indent=2, default=str)[:12000],
        "```",
        "",
        "## 3. TP-Mindestabstand Variante B (PLACE/REPLACE)",
        "",
        f"Regelverletzungen: {len(tp_violations_b)} — `{tp_violations_b}`",
        "",
        "## 4. B-Vorteil nur ein Trade?",
        "",
        f"Primary only-B keys: {only_b_primary}. Einschränkung auf einen Zusatz-Fill: **{b_advantage_single}**.",
        "",
        "## 5. Tests",
        "",
        f"Long pytest rc={long_test.returncode}. Short: {'BLOCKED/partial' if short_blocked else 'rc=0'} (June D107 falls Bundle fehlt).",
        "",
        "## 6. Artefakte",
        "",
        f"- `{OUT / 'trade_chart_review.csv'}`",
        f"- `{OUT / 'trade_chart_review.json'}`",
        "",
        "## Kompakte Chart-Tabelle",
        "",
        "| Datum/Uhrzeit UTC | Variante | ENTRY USDT | SL USDT | TP USDT | EXIT USDT | Ergebnis % |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in chart_table:
        res = row["result_pct"]
        if res == "NULL":
            res_s = "NULL"
        else:
            res_s = str(res)
        md_lines.append(
            f"| {row['datetime_utc']} | {row['variant']} | {row['ENTRY_USDT']} | {row['SL_USDT']} | "
            f"{row['TP_USDT']} | {row['EXIT_USDT']} | {res_s} |"
        )

    REPORT.write_text("\n".join(md_lines), encoding="utf-8")
    (OUT / "phase2l_summary.json").write_text(
        json.dumps(
            {
                "comparisons": comparisons,
                "case_study": case_study,
                "tp_violations_b": tp_violations_b,
                "b_advantage_single_trade_primary": b_advantage_single,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print("PHASE2L_EVIDENCE_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
