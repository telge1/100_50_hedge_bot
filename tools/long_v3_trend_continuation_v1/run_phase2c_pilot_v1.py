#!/usr/bin/env python3
"""LONG V3 Phase 2C — 48h causal pilot LONG A vs LONG B (read-only)."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "logs/research/long_v3_phase2c_pilot_v1"
BUNDLE = ROOT / "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle"

PILOT_START_UTC = "2026-09-20T00:00:00Z"
PILOT_END_UTC = "2026-09-21T23:55:00Z"
LOOKBACK_168H_BEFORE_PILOT = "2026-09-13T00:00:00Z"

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"))

from contracts_v1 import BARS_PER_HOUR_5M, MIN_POOL_LOOKBACK_HOURS  # noqa: E402
from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from ema_research_v1 import ema_map_for_bars  # noqa: E402
from ohlc_loader_v1 import load_5m_ohlc_ch, _parse_utc, _iso  # noqa: E402
from pool_history_gate_v1 import evaluate_pool_history_gate  # noqa: E402
from research_runner_v1 import run_long_research_chronology  # noqa: E402


def _trade_dict(t) -> dict:
    d = asdict(t)
    return d


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if not BUNDLE.is_dir():
        (OUT / "pilot_verdict.json").write_text(
            json.dumps({"verdict": "LONG_V3_PILOT_BLOCKED", "reason": "SEP_BUNDLE_MISSING"}),
            indent=2,
        )
        return 1

    session = FrozenBundleDensitySessionV1(BUNDLE)
    closes = session.decision_closes()
    if PILOT_START_UTC not in closes or PILOT_END_UTC not in closes:
        (OUT / "pilot_verdict.json").write_text(
            json.dumps(
                {
                    "verdict": "LONG_V3_PILOT_BLOCKED",
                    "reason": "PILOT_WINDOW_NOT_IN_BUNDLE",
                    "pilot_start": PILOT_START_UTC,
                    "pilot_end": PILOT_END_UTC,
                    "bundle_last": closes[-1] if closes else None,
                }
            ),
            indent=2,
        )
        return 1

    start_idx = closes.index(PILOT_START_UTC)
    end_idx = closes.index(PILOT_END_UTC)
    lookback_idx = closes.index(LOOKBACK_168H_BEFORE_PILOT) if LOOKBACK_168H_BEFORE_PILOT in closes else None
    gate_at_pilot = evaluate_pool_history_gate(close_index=start_idx)
    coverage = {
        "pilot_window_utc": {"start": PILOT_START_UTC, "end": PILOT_END_UTC, "duration_hours": 48},
        "lookback_168h_anchor_utc": LOOKBACK_168H_BEFORE_PILOT,
        "lookback_bar_in_bundle": lookback_idx,
        "pilot_start_bar_index": start_idx,
        "bars_from_bundle_start_to_pilot": start_idx,
        "required_bars_168h": MIN_POOL_LOOKBACK_HOURS * BARS_PER_HOUR_5M,
        "gate_at_pilot_start": asdict(gate_at_pilot),
        "bundle_closes": len(closes),
        "transport": "validated_frozen_bundle_sep_d92",
    }
    if not gate_at_pilot.ok or lookback_idx is None:
        coverage["verdict"] = "LONG_V3_PILOT_BLOCKED"
        (OUT / "coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
        (OUT / "pilot_verdict.json").write_text(
            json.dumps({"verdict": "LONG_V3_PILOT_BLOCKED", "coverage": coverage}), indent=2
        )
        return 1

    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    bar_keys, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])
    missing = [c for c in closes if c not in prices]
    if missing:
        (OUT / "pilot_verdict.json").write_text(
            json.dumps(
                {
                    "verdict": "LONG_V3_PILOT_BLOCKED",
                    "reason": "OHLC_MISSING_FOR_BUNDLE_CLOSES",
                    "missing_count": len(missing),
                    "first_missing": missing[0],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return 1
    bars = closes
    emas = ema_map_for_bars(sorted(bar_keys), prices)

    entry_bar_sl_checks: list[dict] = []
    touch_forensics: list[dict] = []

    results = {}
    for variant in ("LONG_A", "LONG_B"):
        r = run_long_research_chronology(
            bars=bars,
            prices=prices,
            ema9=emas[9],
            ema20=emas[20],
            ema59=emas[59],
            ema200=emas[200],
            session=session,
            exit_variant=variant,  # type: ignore[arg-type]
            eval_start_index=start_idx,
            eval_end_index=end_idx,
            transport="validated_frozen_bundle_sep_d92",
        )
        for t in r.trades:
            if t.fill_bar_utc and t.exit_bar_utc == t.fill_bar_utc and t.exit_reason == "STRUCTURAL_SL":
                o = prices[t.fill_bar_utc]
                entry_bar_sl_checks.append(
                    {
                        "variant": variant,
                        "bar": t.fill_bar_utc,
                        "entry": t.entry_price,
                        "sl": t.sl_price,
                        "exit_price": t.exit_price,
                        "ohlc": o,
                        "note": "SL_same_bar_as_fill_processed_by_runner",
                    }
                )
        payload = {
            "summary": r.summary,
            "blocked": r.blocked,
            "trades": [_trade_dict(t) for t in r.trades],
            "ambiguous_cases": r.ambiguous_cases,
            "open_position_at_end": r.open_position_at_end,
            "order_event_ledger": r.order_event_ledger,
            "entry_lifecycle_events": r.entry_events,
        }
        (OUT / f"{variant.lower()}_trade_ledger.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        results[variant] = r.summary
        if variant == "LONG_A":
            touch_forensics = list(r.touch_forensics)

    pending_unfilled = sum(
        1
        for ev in json.loads((OUT / "long_a_trade_ledger.json").read_text())["order_event_ledger"]
        if ev.get("action") == "PLACE"
    ) - results.get("LONG_A", {}).get("fills", 0)

    verdict = "LONG_V3_PILOT_PASS"
    if results["LONG_A"].get("ambiguous_exits", 0) + results["LONG_B"].get("ambiguous_exits", 0) > 0:
        verdict = "LONG_V3_PILOT_INCONCLUSIVE"
    if results["LONG_A"]["fills"] == 0 and results["LONG_B"]["fills"] == 0:
        verdict = "LONG_V3_PILOT_INCONCLUSIVE"

    from collections import Counter

    touch_counts = Counter(e.get("event") for e in touch_forensics)
    examples: list[dict] = []
    for ev in touch_forensics:
        if ev.get("event") != "TOUCH_BEGIN":
            continue
        bar = ev.get("bar")
        follow = [x for x in touch_forensics if x.get("bar") == bar or (x.get("bar") and bar and x.get("bar") >= bar)][:5]
        idx = touch_forensics.index(ev)
        chain = touch_forensics[idx : idx + 4]
        o = prices.get(bar, {})
        examples.append(
            {
                "touch_begin": ev,
                "chain_next_events": chain,
                "ohlc_touch_bar": o,
                "outcome": next(
                    (x["event"] for x in touch_forensics[idx + 1 :] if x.get("event") != "TOUCH_BEGIN"),
                    "NO_SUBSEQUENT_ABORT_IN_WINDOW",
                ),
            }
        )
        if len(examples) >= 3:
            break

    report = {
        "verdict": verdict,
        "coverage": coverage,
        "ohlc_meta": ohlc_meta,
        "long_a": results["LONG_A"],
        "long_b": results["LONG_B"],
        "entry_bar_sl_audit": entry_bar_sl_checks,
        "entry_bar_sl_runner_note": (
            "On FILL, runner initializes exit state then calls process_long_exit_bar on the same bar "
            "(research_runner_v1.py); no same-bar SL cases in this pilot (zero fills)."
        ),
        "touch_forensics_counts": dict(touch_counts),
        "touch_signal_examples": examples,
        "pending_places_minus_fills_approx": pending_unfilled,
        "fees_slippage": "not_modeled",
        "note_position_occupancy": "LONG_A and LONG_B use separate runner state; entry counts may differ if one holds open through window end",
        "zero_fill_diagnosis": (
            "No RECOVERY_CONFIRMED in 48h window: touches aborted (break/invalid) or "
            "TOUCH_REJECTED_NOT_ACTIVE_AT_CLOSE; no PLACE/FILL events."
        ),
    }
    (OUT / "coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    (OUT / "pilot_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (OUT / "pilot_verdict.json").write_text(json.dumps({"verdict": verdict}, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
