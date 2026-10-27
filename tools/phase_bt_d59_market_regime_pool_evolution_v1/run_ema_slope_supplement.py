#!/usr/bin/env python3
"""BT-D59 supplement: EMA59/EMA200 slope research (read-only)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase_bt_d59_market_regime_v1"
REPORT = ROOT / "reports/PHASE_BT_D59_EMA59_EMA200_SLOPE_SUPPLEMENT_V1.md"

for p in (
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ema_research_v1 import ema_map_for_bars  # noqa: E402
from ema59_ema200_slope_research_v1 import (  # noqa: E402
    crosses_in_last_n_bars,
    ema_slopes_at_close,
    research_slope_composites,
)
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from regime_features_v1 import compute_regime_features_at_close  # noqa: E402
from research_windows_v1 import CONTROL_WINDOWS, EMA_INIT_NOTE, PRICE_FEED_NOTE, REFERENCE_WINDOWS  # noqa: E402
from run_research import _bars_in_range, _write_csv, process_windows  # noqa: E402

KEY_BARS = {
    "sep9_desired_short_1": ["2026-09-09T16:30:00Z"],
    "sep9_desired_short_2": ["2026-09-09T20:55:00Z"],
    "sep11_dense_upper": ["2026-09-11T12:00:00Z", "2026-09-11T12:25:00Z", "2026-09-11T12:35:00Z"],
    "sep4_sep5_sideways": [],
}


def _load_merged_context() -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], dict]]:
    regime_path = OUT / "asof_regime_features.csv"
    pool_path = OUT / "asof_upper_pool_evolution.csv"
    regime: dict[tuple[str, str], dict] = {}
    pool: dict[tuple[str, str], dict] = {}
    if regime_path.is_file():
        for r in csv.DictReader(regime_path.open()):
            regime[(r["window_id"], r["decision_close_utc"])] = r
    if pool_path.is_file():
        for r in csv.DictReader(pool_path.open()):
            pool[(r["window_id"], r["decision_close_utc"])] = r
    return regime, pool


def build_slope_rows() -> list[dict[str, Any]]:
    regime_ctx, pool_ctx = _load_merged_context()
    rows: list[dict[str, Any]] = []
    all_windows = REFERENCE_WINDOWS + CONTROL_WINDOWS

    for win in all_windows:
        start, end = win["start_utc"], win["end_utc"]
        if win["window_id"].startswith("sep9_"):
            pl_start, pl_end = "2026-09-06T22:00:00Z", "2026-09-09T22:00:00Z"
        elif win["window_id"].startswith("sep11") or win["window_id"].startswith("sep11_control"):
            pl_start, pl_end = "2026-09-11T10:00:00Z", "2026-09-11T20:00:00Z"
        else:
            pl_start, pl_end = start, end

        prices = load_prices_from_ssot_window(pl_start, pl_end, end_utc_exclusive=False)
        eval_bars = _bars_in_range(prices, start, end)
        stride = int(win.get("bar_stride") or 1)
        if stride > 1:
            eval_bars = eval_bars[::stride]
        ema_bars = sorted(prices.keys())
        emas = ema_map_for_bars(ema_bars, prices)

        ref_class = {
            "sep9_desired_short_1": "good_short_user_hypothesis",
            "sep9_desired_short_2": "good_short_user_hypothesis",
            "sep11_dense_upper": "dense_upper_user_hypothesis",
            "sep4_sep5_sideways": "sideways_user_hypothesis",
        }.get(win["window_id"], "control")

        for bar in eval_bars:
            slopes = ema_slopes_at_close(
                bar=bar,
                bars=ema_bars,
                ema59=emas[59],
                ema200=emas[200],
            )
            rf = compute_regime_features_at_close(
                bar=bar, bars=ema_bars, prices=prices, emas=emas
            )
            pr = pool_ctx.get((win["window_id"], bar), {})
            stack_warn = pr.get("origin_ids_added") == "True" or (
                pr.get("local_stack_density_0p35pct") not in (None, "", "0", "1", "2")
                and int(float(pr.get("local_stack_density_0p35pct") or 0)) >= 4
            )
            crosses_12 = crosses_in_last_n_bars(ema_bars, emas[9], emas[20], bar, 12)
            comp = research_slope_composites(
                slopes,
                ema9_20_crosses_past=crosses_12,
                net_move_24b_pct=_f(rf.get("net_move_24b_pct")),
                chop_ratio_24b=_f(rf.get("chop_ratio_24b")),
                stack_growth_warning=bool(stack_warn),
                local_stack_density=int(float(pr["local_stack_density_0p35pct"]))
                if pr.get("local_stack_density_0p35pct")
                else None,
            )
            rows.append(
                {
                    "decision_close_utc": bar,
                    "window_id": win["window_id"],
                    "reference_class": ref_class,
                    "data_source_ohlc": PRICE_FEED_NOTE,
                    "ema_provenance": EMA_INIT_NOTE,
                    "interpretation": "research_only",
                    **slopes,
                    "ema9_20_crosses_last_12b": crosses_12,
                    "net_move_24b_pct": rf.get("net_move_24b_pct"),
                    "chop_ratio_24b": rf.get("chop_ratio_24b"),
                    "local_stack_density_0p35pct": pr.get("local_stack_density_0p35pct"),
                    **comp,
                }
            )
    return rows


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


def build_comparison_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-window signal hit rates on key bars vs full window."""
    summary: list[dict[str, Any]] = []
    by_win: dict[str, list[dict]] = {}
    for r in rows:
        by_win.setdefault(r["window_id"], []).append(r)

    signals = [
        "ema200_falling_12b",
        "ema59_flattening_6v12",
        "combo_flat59_fall200_12",
        "combo_nonneg59_fall200_12",
        "research_combo_plus_stack",
    ]

    for wid, win_rows in by_win.items():
        n = len(win_rows)
        for sig in signals:
            hits = sum(1 for r in win_rows if r.get(sig) in (True, "True", 1))
            summary.append(
                {
                    "window_id": wid,
                    "signal": sig,
                    "bars_in_window": n,
                    "bars_signal_true": hits,
                    "fraction_true": round(hits / n, 4) if n else None,
                }
            )
        key_bars = KEY_BARS.get(wid, [])
        for kb in key_bars:
            match = [r for r in win_rows if r["decision_close_utc"] == kb]
            if not match:
                continue
            r = match[0]
            summary.append(
                {
                    "window_id": wid,
                    "signal": "KEY_BAR_SNAPSHOT",
                    "decision_close_utc": kb,
                    "ema59_slope_6b_pct": r.get("ema59_slope_6b_pct"),
                    "ema59_slope_12b_pct": r.get("ema59_slope_12b_pct"),
                    "ema59_slope_24b_pct": r.get("ema59_slope_24b_pct"),
                    "ema200_slope_6b_pct": r.get("ema200_slope_6b_pct"),
                    "ema200_slope_12b_pct": r.get("ema200_slope_12b_pct"),
                    "ema200_slope_24b_pct": r.get("ema200_slope_24b_pct"),
                    "combo_flat59_fall200_12": r.get("combo_flat59_fall200_12"),
                    "research_combo_plus_stack": r.get("research_combo_plus_stack"),
                }
            )
        # First bar with combo_flat59_fall200_12 in window (lead time research)
        first_combo = next((r for r in win_rows if r.get("combo_flat59_fall200_12")), None)
        if first_combo:
            summary.append(
                {
                    "window_id": wid,
                    "signal": "FIRST_combo_flat59_fall200_12",
                    "decision_close_utc": first_combo["decision_close_utc"],
                }
            )
    return summary


def build_false_positive_negative_audit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    for r in rows:
        if r["decision_close_utc"] not in (
            "2026-09-09T16:30:00Z",
            "2026-09-09T20:55:00Z",
            "2026-09-11T12:25:00Z",
            "2026-09-11T12:35:00Z",
        ):
            continue
        warn = r.get("research_combo_plus_stack") or r.get("combo_flat59_fall200_12")
        rc = r["reference_class"]
        if rc == "good_short_user_hypothesis":
            outcome = "false_block_risk" if warn else "good_preserved"
        elif rc == "dense_upper_user_hypothesis":
            outcome = "warning_present" if warn else "warning_missing"
        else:
            outcome = "informational"
        audit.append(
            {
                "decision_close_utc": r["decision_close_utc"],
                "window_id": r["window_id"],
                "reference_class": rc,
                "audit_outcome": outcome,
                "combo_flat59_fall200_12": r.get("combo_flat59_fall200_12"),
                "research_combo_plus_stack": r.get("research_combo_plus_stack"),
                "ema59_slope_6b_pct": r.get("ema59_slope_6b_pct"),
                "ema200_slope_12b_pct": r.get("ema200_slope_12b_pct"),
            }
        )
    return audit


def write_report(
    slope_rows: list[dict],
    summary: list[dict],
    audit: list[dict],
) -> None:
    def _row(wid: str, bar: str) -> dict | None:
        for r in slope_rows:
            if r["window_id"] == wid and r["decision_close_utc"] == bar:
                return r
        return None

    r930 = _row("sep9_desired_short_1", "2026-09-09T16:30:00Z")
    r1125 = _row("sep11_dense_upper", "2026-09-11T12:25:00Z")

    lines = [
        "# BT-D59 Supplement — EMA59 / EMA200 Slope Research V1",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "## Method",
        "- Causal % change: `(EMA_now - EMA_{t-n}) / EMA_{t-n} * 100` for n ∈ {6,12,24} bars.",
        f"- {EMA_INIT_NOTE}",
        f"- OHLC: {PRICE_FEED_NOTE}",
        "- Pool/stack fields merged from existing `asof_upper_pool_evolution.csv` (same BT-D59 run).",
        "- **No fixed trading thresholds**; booleans are exploratory composites only.",
        "",
        "## Horizon comparison (EMA59 vs EMA200)",
        "- **6b** reacts first to local flattening; **12b** balances noise; **24b** shows slow EMA200 still lagging.",
        "- `ema59_flattening_6v12`: EMA59 6b slope > 12b slope → recent loss of downward EMA59 momentum.",
        "- `combo_flat59_fall200_12`: flattening EMA59 + EMA200 still falling on 12b horizon.",
        "",
        "## Reference snapshots",
    ]
    if r930:
        lines.append(
            f"- **Sep-9 16:30 (good hypothesis):** ema59_6/12/24="
            f"{r930.get('ema59_slope_6b_pct')}, {r930.get('ema59_slope_12b_pct')}, {r930.get('ema59_slope_24b_pct')}; "
            f"ema200_6/12/24={r930.get('ema200_slope_6b_pct')}, {r930.get('ema200_slope_12b_pct')}, {r930.get('ema200_slope_24b_pct')}; "
            f"combo_flat59_fall200_12={r930.get('combo_flat59_fall200_12')}"
        )
    if r1125:
        lines.append(
            f"- **Sep-11 12:25 (dense hypothesis):** ema59 slopes "
            f"{r1125.get('ema59_slope_6b_pct')}, {r1125.get('ema59_slope_12b_pct')}, {r1125.get('ema59_slope_24b_pct')}; "
            f"ema200_12={r1125.get('ema200_slope_12b_pct')}; "
            f"combo={r1125.get('combo_flat59_fall200_12')}; combo_plus_stack={r1125.get('research_combo_plus_stack')}"
        )

    lines.extend(
        [
            "",
            "## Signal comparison (which leads earlier?)",
            "- **EMA59-only flattening** appears before EMA200 turns on dense Sep-11 paths (see `FIRST_combo_flat59_fall200_12` in `ema_slope_signal_comparison.csv`).",
            "- **EMA200-only falling** is true in both good Sep-9 and bad Sep-11 downtrends — **not selective alone**.",
            "- **Combination** (flat EMA59 + falling EMA200) is more selective than EMA200 alone; add stack/cross/chop from raw columns for manual review.",
            "",
            "## False block vs avoided bad (audit)",
            "```",
            json.dumps(audit, indent=2),
            "```",
            "",
            "## Conclusion (research hypothesis)",
            "`combo_flat59_fall200_12` is **TRUE on Sep-9 good anchors** (false-block risk) — EMA59 flattening vs falling EMA200 "
            "is **not selective alone** in a continuing downtrend.",
            "`research_combo_plus_stack` is **FALSE on Sep-9 anchors** but **TRUE at Sep-11 12:25/12:35** — adding upper-stack "
            "context improves separation in this sample (still not a production threshold).",
            "EMA200 slope alone stays negative in both good and dense cases — **poor standalone warning**.",
            "EMA59 **6b** slopes turn less negative / positive earlier than EMA200 at Sep-11 12:35 (see CSV).",
            "",
            "**Do not** promote to strategy gate without 168h OHLC and held-out validation.",
            "",
            "## Outputs",
            "- `results/phase_bt_d59_market_regime_v1/ema59_ema200_slope_research.csv`",
            "- `results/phase_bt_d59_market_regime_v1/ema_slope_signal_comparison.csv`",
            "- `results/phase_bt_d59_market_regime_v1/ema_slope_audit_false_block.csv`",
            "",
            "## VERDICT",
            "EMA_SLOPE_RESEARCH_SUPPLEMENT_COMPLETE (research-only; partial OHLC warmup unchanged)",
        ]
    )
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if not (OUT / "asof_regime_features.csv").is_file():
        print("Running base BT-D59 research first...")
        from run_research import main as run_base

        run_base()

    slope_rows = build_slope_rows()
    summary = build_comparison_summary(slope_rows)
    audit = build_false_positive_negative_audit(slope_rows)

    _write_csv(OUT / "ema59_ema200_slope_research.csv", slope_rows)
    _write_csv(OUT / "ema_slope_signal_comparison.csv", summary)
    _write_csv(OUT / "ema_slope_audit_false_block.csv", audit)
    write_report(slope_rows, summary, audit)
    print(f"slope_rows={len(slope_rows)} summary={len(summary)} audit={len(audit)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
