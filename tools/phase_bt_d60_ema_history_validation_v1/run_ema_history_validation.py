#!/usr/bin/env python3
"""BT-D60: mandatory EMA history validation before regime re-evaluation."""

from __future__ import annotations

import csv
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tools/phase_bt_d60_ema_history_validation_v1"
OUT = ROOT / "results/phase_bt_d60_ema_history_v1"
REPORT = ROOT / "reports/PHASE_BT_D60_EMA_HISTORY_REQUIREMENT_V1.md"

for p in (
    PKG,
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bar_integrity_v1 import audit_bar_series  # noqa: E402
from convergence_v1 import summarize_convergence  # noqa: E402
from ema_definitions_v1 import (  # noqa: E402
    BT_D59_FIRST_CLOSE_DOC,
    EMA_PERIODS,
    LIQUIDITY_CONTEXT_DOC,
    TRP_RESEARCH_CHARTS_DOC,
    ema_series_for_path,
    ema_at_index,
)
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from validation_config_v1 import (  # noqa: E402
    EMA200_CONVERGENCE_ABS_TOL,
    EMA200_CONVERGENCE_REL_TOL,
    FIRST_DECISION_CLOSE,
    MIN_BARS_BEFORE_SLOPE,
    POOL_CONTEXT_WARMUP_HOURS,
    PROBE_DECISION_CLOSES,
    REFERENCE_WARMUP_HOURS,
    SLOPE_LOOKBACKS,
    WARMUP_HOURS_CANDIDATES,
)


def _ssot_72h_prices(probe_end: str) -> dict[str, dict[str, float]]:
    from price_feed_v1 import load_prices_from_ssot_window

    end_dt = _parse_utc(probe_end)
    start_dt = end_dt - timedelta(hours=72)
    return load_prices_from_ssot_window(_iso(start_dt), probe_end, end_utc_exclusive=False)


def _path_divergence_sample(
    bar_keys: list[str], prices: dict[str, dict[str, float]], probes: list[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for probe in probes[:4]:
        if probe not in prices:
            continue
        idx = bar_keys.index(probe)
        closes = [float(prices[b]["c"]) for b in bar_keys[: idx + 1]]
        for period in (9, 200):
            trp = ema_at_index(ema_series_for_path("trp_research_charts", closes, period), idx)
            d59 = ema_at_index(ema_series_for_path("bt_d59_first_close", closes, period), idx)
            if trp is None or d59 is None:
                continue
            rows.append(
                {
                    "probe": probe,
                    "period": period,
                    "trp_research_charts": trp,
                    "bt_d59_first_close": d59,
                    "abs_diff": abs(trp - d59),
                }
            )
    return rows


def _slope_warmth_checks(bar_keys: list[str], prices: dict[str, dict[str, float]]) -> dict[str, Any]:
    failures: list[str] = []
    for probe in PROBE_DECISION_CLOSES:
        if probe not in prices:
            failures.append(f"{probe}: missing bar")
            continue
        idx = bar_keys.index(probe)
        closes = [float(prices[b]["c"]) for b in bar_keys[: idx + 1]]
        for period in (59, 200):
            series = ema_series_for_path("trp_research_charts", closes, period)
            min_bars = MIN_BARS_BEFORE_SLOPE[period]
            if idx + 1 < min_bars or ema_at_index(series, idx) is None:
                failures.append(f"{probe}: ema{period} not warmed ({idx + 1} bars < {min_bars})")
                continue
            for lb in SLOPE_LOOKBACKS:
                if idx < lb:
                    failures.append(f"{probe}: ema{period} slope {lb}b needs {lb} prior bars")
                elif ema_at_index(series, idx - lb) is None:
                    failures.append(f"{probe}: ema{period} slope {lb}b prior not warmed")
    return {"ok": len(failures) == 0, "failures": failures}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    first = _parse_utc(FIRST_DECISION_CLOSE)
    load_start = first - timedelta(hours=REFERENCE_WARMUP_HOURS + 24)
    load_end = max(_parse_utc(p) for p in PROBE_DECISION_CLOSES)

    try:
        bar_keys, prices, ohlc_meta = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    except Exception as exc:
        manifest = {
            "phase": "BT-D60",
            "verdict": "EMA_HISTORY_INSUFFICIENT",
            "regime_gate": "UNCERTAIN",
            "short_approval": False,
            "error": str(exc),
            "bt_d59_ema_results": "PRELIMINARY_ONLY",
        }
        (OUT / "ema_history_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        REPORT.write_text(
            f"# BT-D60 — EMA History Requirement V1\n\n**VERDICT:** `EMA_HISTORY_INSUFFICIENT` (OHLC load failed)\n\n```\n{exc}\n```\n",
            encoding="utf-8",
        )
        print(json.dumps(manifest, indent=2))
        return 2

    integrity = audit_bar_series(bar_keys, as_of_utc=_iso(load_end))
    conv = summarize_convergence(bar_keys, prices, PROBE_DECISION_CLOSES)
    slopes = _slope_warmth_checks(bar_keys, prices)
    path_div = _path_divergence_sample(bar_keys, prices, PROBE_DECISION_CLOSES)

    ssot_rows: list[dict[str, Any]] = []
    ssot_ok = True
    for probe in PROBE_DECISION_CLOSES:
        if probe not in prices:
            continue
        try:
            ssot = _ssot_72h_prices(probe)
        except Exception as exc:
            ssot_rows.append({"probe": probe, "ok": False, "error": str(exc)})
            ssot_ok = False
            continue
        if probe not in ssot:
            ssot_rows.append({"probe": probe, "ok": False, "reason": "probe_not_in_ssot_window"})
            ssot_ok = False
            continue
        idx = bar_keys.index(probe)
        closes_ch = [float(prices[b]["c"]) for b in bar_keys[: idx + 1]]
        ssot_keys = sorted(ssot.keys())
        closes_ssot = [float(ssot[b]["c"]) for b in ssot_keys if b <= probe]
        if not closes_ssot:
            ssot_ok = False
            continue
        trp_ch = ema_at_index(ema_series_for_path("trp_research_charts", closes_ch, 200), idx)
        trp_ssot = ema_at_index(
            ema_series_for_path("trp_research_charts", closes_ssot, 200), len(closes_ssot) - 1
        )
        if trp_ch is None or trp_ssot is None:
            ssot_rows.append({"probe": probe, "ok": False, "reason": "ema200_not_computed"})
            ssot_ok = False
            continue
        diff = abs(trp_ch - trp_ssot)
        ok = diff <= max(EMA200_CONVERGENCE_ABS_TOL, EMA200_CONVERGENCE_REL_TOL * max(abs(trp_ch), 1e-8))
        ssot_rows.append(
            {
                "probe": probe,
                "ema200_ch_full_history": trp_ch,
                "ema200_ssot_72h_feed": trp_ssot,
                "abs_diff": diff,
                "ok": ok,
                "ssot_bars": len(closes_ssot),
            }
        )
        if not ok:
            ssot_ok = False

    history_ok = (
        integrity["ok"]
        and conv["all_candidates_converge"]
        and slopes["ok"]
        and ohlc_meta.get("bars_loaded", 0) > 0
        and not ohlc_meta.get("out_of_import_bounds")
    )
    # SSOT 72h feed must NOT be used for regime EMA until it matches long-history reference.
    if not ssot_ok:
        history_ok = False

    verdict = "EMA_HISTORY_VALIDATED" if history_ok else "EMA_HISTORY_INSUFFICIENT"
    manifest: dict[str, Any] = {
        "phase": "BT-D60",
        "verdict": verdict,
        "regime_gate": "UNCERTAIN" if not history_ok else "READY_FOR_REGIME_GEGENPROBE",
        "short_approval": False,
        "bt_d59_ema_results": "PRELIMINARY_ONLY" if not history_ok else "SUPERSEDED_BY_D60_TRP_PATH",
        "pool_context_hours": POOL_CONTEXT_WARMUP_HOURS,
        "ohlc_warmup_reference_hours": REFERENCE_WARMUP_HOURS,
        "warmup_candidates_hours": list(WARMUP_HOURS_CANDIDATES),
        "ema_definition_primary": "trp_research_charts",
        "ema_docs": {
            "trp_research_charts": TRP_RESEARCH_CHARTS_DOC,
            "bt_d59_first_close": BT_D59_FIRST_CLOSE_DOC,
            "liquidity_context_engine": LIQUIDITY_CONTEXT_DOC,
        },
        "convergence_tolerances": {
            "ema200": {"abs": EMA200_CONVERGENCE_ABS_TOL, "rel": EMA200_CONVERGENCE_REL_TOL},
        },
        "ohlc_meta": ohlc_meta,
        "bar_integrity": integrity,
        "convergence": conv,
        "slope_warmth": slopes,
        "path_divergence_sample": path_div,
        "ssot_72h_feed_vs_ch": {"all_ok": ssot_ok, "rows": ssot_rows},
        "first_decision_close": FIRST_DECISION_CLOSE,
        "probe_decision_closes": PROBE_DECISION_CLOSES,
    }

    (OUT / "ema_history_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (OUT / "convergence_by_probe.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "warmup_hours",
                "probe",
                "bars_in_slice",
                "ema200_ref",
                "ema200_warm",
                "ema200_abs_diff",
                "ema200_ok",
                "ema59_ok",
                "ok",
            ],
            extrasaction="ignore",
        )
        w.writeheader()
        for block in conv["candidates"]:
            for row in block["rows"]:
                w.writerow({**row, "warmup_hours": block["warmup_hours"]})

    lines = [
        "# BT-D60 — EMA History Requirement V1",
        "",
        f"**VERDICT:** `{verdict}`",
        f"**Regime gate:** `{manifest['regime_gate']}` (no short approval in this phase)",
        "",
        "## Separation A / B",
        f"- **A — Pool context:** ≥{POOL_CONTEXT_WARMUP_HOURS}h ACTIVE origins (unchanged; not re-validated here).",
        f"- **B — OHLC EMA warmup:** ClickHouse 5m load from `{_iso(load_start)}` to `{_iso(load_end)}` "
        f"(reference slice {REFERENCE_WARMUP_HOURS}h vs candidates {list(WARMUP_HOURS_CANDIDATES)}).",
        "",
        "## EMA definition (binding for D60+)",
        f"- **Primary:** {TRP_RESEARCH_CHARTS_DOC}",
        f"- **BT-D59 (preliminary):** {BT_D59_FIRST_CLOSE_DOC}",
        f"- **Liquidity context:** {LIQUIDITY_CONTEXT_DOC}",
        "",
        "## Convergence tolerance (EMA200 / EMA59)",
        f"- `max(abs_diff, rel_tol * |ref|)` with abs={EMA200_CONVERGENCE_ABS_TOL}, rel={EMA200_CONVERGENCE_REL_TOL}",
        "- Reference EMA at probe = TRP path over **all loaded bars** from series start through probe.",
        "",
        "## Checks",
        f"- Bar integrity: `{integrity['ok']}` (gaps={integrity['missing_interior_gaps']}, dups={integrity['duplicate_timestamps']})",
        f"- Warmup convergence (72/168/336h vs full): `{conv['all_candidates_converge']}`",
        f"- Slope warmth (EMA59/200 @ 6/12/24b): `{slopes['ok']}`",
        f"- SSOT `price_feed_v1` ≤72h EMA200 vs CH full history: `{ssot_ok}`",
        "",
        "## BT-D59 status",
        manifest["bt_d59_ema_results"],
        "",
        "## Outputs",
        f"- `{OUT.relative_to(ROOT)}/ema_history_manifest.json`",
        f"- `{OUT.relative_to(ROOT)}/convergence_by_probe.csv`",
        "",
    ]
    if history_ok:
        lines.append(
            "Next: run `run_regime_gegenprobe.py` (BT-D60 regime re-evaluation with TRP EMA + long OHLC only)."
        )
    else:
        lines.append(
            "Do **not** run regime Gegenprobe until `EMA_HISTORY_VALIDATED`. "
            "Regime remains **UNCERTAIN**; no silent EMA substitutes."
        )

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"verdict": verdict, "report": str(REPORT)}, indent=2))
    return 0 if history_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
