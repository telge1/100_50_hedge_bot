#!/usr/bin/env python3
"""LONG V3 Phase 2M — ledger integrity + extended A/B validation."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2m_validation_v1"
REPORT = ROOT / "reports/LONG_V3_PHASE2M_LEDGER_AND_EXTENDED_AB_VALIDATION_V1.md"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"
YAML = ROOT / "configs/long_v3_trend_continuation_research_v1.yaml"

KNOWN_WINDOWS = {
    "known_primary_16_17": ("2026-09-16T00:00:00Z", "2026-09-17T23:55:00Z"),
    "known_independent_18_19": ("2026-09-18T00:00:00Z", "2026-09-19T23:55:00Z"),
    "known_control_20_21": ("2026-09-20T00:00:00Z", "2026-09-21T23:55:00Z"),
}
EXTENDED_WINDOWS = {
    "extended_10_11": ("2026-09-10T00:00:00Z", "2026-09-11T23:55:00Z"),
    "extended_14_15": ("2026-09-14T00:00:00Z", "2026-09-15T23:55:00Z"),
    "extended_22_23": ("2026-09-22T00:00:00Z", "2026-09-23T23:55:00Z"),
    "extended_24_25": ("2026-09-24T00:00:00Z", "2026-09-25T23:55:00Z"),
    "extended_26_27": ("2026-09-26T00:00:00Z", "2026-09-27T23:55:00Z"),
    "extended_28_29": ("2026-09-28T00:00:00Z", "2026-09-29T23:55:00Z"),
}

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))

from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from ema_research_v1 import ema_map_for_bars  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_phase2l_evidence_v1 import audit_b_place_replace_tp  # noqa: E402
from long_phase2m_ledger_integrity_v1 import (  # noqa: E402
    collect_b_repricing_events,
    enrich_repricing_with_outcomes,
    max_consecutive_sl,
    reconstruct_trade_lifecycle,
)
from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from ohlc_loader_v1 import load_5m_ohlc_ch, _iso, _parse_utc  # noqa: E402
from pool_history_gate_v1 import evaluate_pool_history_gate  # noqa: E402
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


def _window_ok(closes: list[str], w0: str, w1: str) -> tuple[bool, str, int, int]:
    try:
        i0, i1 = closes.index(w0), closes.index(w1)
    except ValueError as e:
        return False, f"CLOSE_NOT_IN_BUNDLE:{e}", -1, -1
    gate = evaluate_pool_history_gate(close_index=i0)
    if not gate.ok:
        return False, gate.reason_code, i0, i1
    return True, "POOL_HISTORY_OK", i0, i1


def _run_pair(session, closes, prices, emas, i0, i1, cfg_a, cfg_b):
    kw = dict(
        bars=closes,
        prices=prices,
        ema9=emas[9],
        ema20=emas[20],
        ema59=emas[59],
        ema200=emas[200],
        session=session,
        exit_variant="LONG_A",
        eval_start_index=i0,
        eval_end_index=i1,
    )
    return run_long_research_v2d(entry_config=cfg_a, **kw), run_long_research_v2d(entry_config=cfg_b, **kw)


def _fill_keys(trades) -> set[str]:
    return {f"{t.fill_bar_utc}|{repr(float(t.entry_price))}" for t in trades}


def _summarize_window(name: str, ra, rb, session) -> dict:
    ka, kb = _fill_keys(ra.trades), _fill_keys(rb.trades)
    resolved_a = [t for t in ra.trades if t.gross_return_pct is not None]
    resolved_b = [t for t in rb.trades if t.gross_return_pct is not None]
    sum_a = round(sum(t.gross_return_pct for t in resolved_a), 6)
    sum_b = round(sum(t.gross_return_pct for t in resolved_b), 6)
    tp_v = audit_b_place_replace_tp(rb.order_event_ledger, session)
    worse_shared = []
    for key in ka & kb:
        ta = next(t for t in ra.trades if f"{t.fill_bar_utc}|{repr(float(t.entry_price))}" == key)
        tb = next(t for t in rb.trades if f"{t.fill_bar_utc}|{repr(float(t.entry_price))}" == key)
        if (tb.gross_return_pct or 0) < (ta.gross_return_pct or 0) - 1e-9:
            worse_shared.append({"key": key, "A": ta.gross_return_pct, "B": tb.gross_return_pct})
    return {
        "window": name,
        "A_trades": len(ra.trades),
        "B_trades": len(rb.trades),
        "shared": sorted(ka & kb),
        "only_A": sorted(ka - kb),
        "only_B": sorted(kb - ka),
        "A_tp": ra.summary.get("tp_exits"),
        "B_tp": rb.summary.get("tp_exits"),
        "A_sl": ra.summary.get("sl_exits"),
        "B_sl": rb.summary.get("sl_exits"),
        "A_sum_gross_pct": sum_a,
        "B_sum_gross_pct": sum_b,
        "delta_sum_gross_pct": round(sum_b - sum_a, 6),
        "A_replaces": ra.summary.get("replaces"),
        "B_replaces": rb.summary.get("replaces"),
        "B_repricing_count": len(rb.repricing_audits),
        "tp_violations_B": tp_v,
        "B_worse_shared_fills": worse_shared,
        "max_consecutive_sl_A": max_consecutive_sl(ra.trades),
        "max_consecutive_sl_B": max_consecutive_sl(rb.trades),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ly = load_long_research_yaml(YAML)
    session = FrozenBundleDensitySessionV1(BUNDLE)
    closes = session.decision_closes()
    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])
    emas = ema_map_for_bars(sorted(prices.keys()), prices)

    cfg_a = _cfg(ly.support, ly.max_risk_pct, "FROZEN_AT_PLACE_V2J")
    cfg_b = _cfg(ly.support, ly.max_risk_pct, "DYNAMIC_UPPER_GROWTH_V2K")

    all_windows = {**KNOWN_WINDOWS, **EXTENDED_WINDOWS}
    window_results: dict[str, dict] = {}
    blocked: list[str] = []
    all_repricing: list[dict] = []
    tp_violations_all: list[dict] = []
    ledger_recon = None
    trades_a_all = []
    trades_b_all = []

    for wname, (w0, w1) in all_windows.items():
        ok, reason, i0, i1 = _window_ok(closes, w0, w1)
        if not ok:
            blocked.append(f"{wname}:{reason}")
            continue
        ra, rb = _run_pair(session, closes, prices, emas, i0, i1, cfg_a, cfg_b)
        if wname == "known_primary_16_17":
            ledger_recon = reconstruct_trade_lifecycle(
                ledger=ra.order_event_ledger,
                exit_ledger=ra.order_event_ledger,
                fill_utc="2026-09-17T16:30:00Z",
                entry_price=0.08168,
                sl_price=0.08064,
            )
        sm = _summarize_window(wname, ra, rb, session)
        window_results[wname] = sm
        tp_violations_all.extend({**v, "window": wname} for v in sm["tp_violations_B"])
        trades_a_all.extend(ra.trades)
        trades_b_all.extend(rb.trades)
        rep = collect_b_repricing_events(rb.order_event_ledger, rb.repricing_audits, window=wname)
        all_repricing.extend(enrich_repricing_with_outcomes(rep, rb.trades, rb.order_event_ledger))

    extended_only = {k: v for k, v in window_results.items() if k.startswith("extended_")}
    ext_delta = sum(v["delta_sum_gross_pct"] for v in extended_only.values())
    ext_only_b = sum(len(v["only_B"]) for v in extended_only.values())
    known_delta = sum(v["delta_sum_gross_pct"] for k, v in window_results.items() if k.startswith("known_"))
    total_a = sum(v["A_sum_gross_pct"] for v in window_results.values())
    total_b = sum(v["B_sum_gross_pct"] for v in window_results.values())

    long_test = subprocess.run(
        [sys.executable, "-m", "pytest", str(PKG / "tests"), "-q", "--tb=no"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )

    ledger_status = ledger_recon.get("status") if ledger_recon else "UNRESOLVED"
    if blocked:
        verdict = "DATA_BLOCKED"
    elif ledger_status != "RESOLVED":
        verdict = "LEDGER_UNRESOLVED"
    elif not extended_only:
        verdict = "INSUFFICIENT_EVIDENCE"
    else:
        verdict = "VALIDATED_EVIDENCE"

    repricing_with_fill = [r for r in all_repricing if r.get("filled_after_reprice")]
    repricing_no_fill = [r for r in all_repricing if not r.get("filled_after_reprice")]

    payload = {
        "verdict": verdict,
        "ohlc_meta": ohlc_meta,
        "blocked_windows": blocked,
        "ledger_reconstruction_2026_09_17_1630_A": ledger_recon,
        "window_results": window_results,
        "extended_summary": {
            "windows_count": len(extended_only),
            "sum_delta_gross_pct": round(ext_delta, 6),
            "extra_b_fills": ext_only_b,
        },
        "known_summary": {"sum_delta_gross_pct": round(known_delta, 6)},
        "totals_all_windows": {
            "A_trades": sum(v["A_trades"] for v in window_results.values()),
            "B_trades": sum(v["B_trades"] for v in window_results.values()),
            "A_sum_gross_pct": round(total_a, 6),
            "B_sum_gross_pct": round(total_b, 6),
            "delta_sum_gross_pct": round(total_b - total_a, 6),
            "max_consecutive_sl_A": max_consecutive_sl(trades_a_all),
            "max_consecutive_sl_B": max_consecutive_sl(trades_b_all),
            "sample_note": "Addierte, nicht kapitalisierte Brutto-Trade-Renditen; keine Kontorendite.",
        },
        "repricing_all_B": all_repricing,
        "repricing_filled": repricing_with_fill,
        "repricing_not_filled": repricing_no_fill,
        "tp_violations_B": tp_violations_all,
        "tests": {"long_pytest_rc": long_test.returncode},
    }

    (OUT / "phase2m_results.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    with open(OUT / "window_comparison.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "window",
                "A_trades",
                "B_trades",
                "delta_sum_gross_pct",
                "only_B_count",
                "only_A_count",
                "B_repricing_count",
            ],
        )
        w.writeheader()
        for k, v in window_results.items():
            w.writerow(
                {
                    "window": k,
                    "A_trades": v["A_trades"],
                    "B_trades": v["B_trades"],
                    "delta_sum_gross_pct": v["delta_sum_gross_pct"],
                    "only_B_count": len(v["only_B"]),
                    "only_A_count": len(v["only_A"]),
                    "B_repricing_count": v["B_repricing_count"],
                }
            )

    md = [
        "# LONG V3 Phase 2M — Ledger & Extended A/B",
        "",
        f"**Status:** `{verdict}`",
        "",
        "## 1. Ledger 17.09.2026 16:30 A",
        "",
        f"Reconstruction: **{ledger_status}**",
        "",
        "```json",
        json.dumps(ledger_recon, indent=2, default=str)[:14000],
        "```",
        "",
        "## 2. Fenster (known + extended)",
        "",
        "```json",
        json.dumps(window_results, indent=2, default=str)[:10000],
        "```",
        "",
        "## 3. Totals & Repricing",
        "",
        "```json",
        json.dumps(
            {
                "totals": payload["totals_all_windows"],
                "extended": payload["extended_summary"],
                "repricing_all_B": all_repricing,
            },
            indent=2,
            default=str,
        )[:12000],
        "```",
        "",
        f"TP-Regelverletzungen B: **{len(tp_violations_all)}**",
        "",
        f"Long pytest: rc={long_test.returncode}",
        "",
        "---",
        "STOP.",
    ]
    REPORT.write_text("\n".join(md), encoding="utf-8")
    print(verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
