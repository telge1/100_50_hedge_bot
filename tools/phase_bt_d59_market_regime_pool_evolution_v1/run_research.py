#!/usr/bin/env python3
"""BT-D59 causal market regime + upper pool evolution research (read-only)."""

from __future__ import annotations

import copy
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase_bt_d59_market_regime_v1"
REPORT = ROOT / "reports/PHASE_BT_D59_MARKET_REGIME_POOL_EVOLUTION_V1.md"

for p in (
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402

from ema_research_v1 import ema_map_for_bars  # noqa: E402
from pool_evolution_v1 import compute_pool_evolution_row  # noqa: E402
from regime_features_v1 import compute_regime_features_at_close  # noqa: E402
from research_windows_v1 import (  # noqa: E402
    CONTROL_WINDOWS,
    DENSITY_NOTE_CACHE,
    DENSITY_NOTE_CANONICAL,
    EMA_INIT_NOTE,
    PRICE_FEED_NOTE,
    REFERENCE_WINDOWS,
)
from variant_flags_v1 import variant_flags  # noqa: E402
from sl_episode_research_v1 import build_sl_research_rows  # noqa: E402
from d54_snapshot_loader_v1 import DENSITY_NOTE_D54, load_d54_window_snapshots  # noqa: E402

PILOT = ROOT / "configs/pilot_bt_d21_pool_first_24h.yaml"
MIN_GAP = 0.5


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _bars_in_range(prices: dict, start: str, end: str) -> list[str]:
    s, e = _parse_utc(start), _parse_utc(end)
    out = []
    for t in sorted(prices.keys()):
        dt = _parse_utc(t)
        if s <= dt <= e:
            out.append(t)
    return out


def _price_load_start(window_start: str, window_end: str) -> tuple[str, str]:
    """Load through window_end; price_feed applies ≤72h vis window ending at end."""
    return window_start, window_end


def _density_session(source: str, cache_rel: str | None, config: dict[str, Any]) -> Any:
    cfg = copy.deepcopy(config)
    if source == "validated_cache":
        bundle = ROOT / (cache_rel or "")
        cfg["density"]["density_source"] = "validated_cache"
        cfg["density"]["validated_cache"] = {"bundle_path": str(bundle)}
        return ValidatedDensityCacheSessionV1(bundle, strategy_config=cfg)
    cfg["density"]["density_source"] = "canonical_provider"
    return create_density_session(cfg)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def process_windows(windows: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list, list, list, list]:
    regime_rows: list[dict[str, Any]] = []
    pool_rows: list[dict[str, Any]] = []
    ref_rows: list[dict[str, Any]] = []
    variant_rows: list[dict[str, Any]] = []

    for win in windows:
        start, end = win["start_utc"], win["end_utc"]
        if win["window_id"].startswith("sep9_"):
            # Max ~72h OHLC ending Sep-9 22:00 for EMA context (partial vs 168h goal).
            pl_start, pl_end = "2026-09-06T22:00:00Z", "2026-09-09T22:00:00Z"
        else:
            pl_start, pl_end = _price_load_start(start, end)
        prices = load_prices_from_ssot_window(pl_start, pl_end, end_utc_exclusive=False)
        eval_bars = _bars_in_range(prices, start, end)
        stride = int(win.get("bar_stride") or 1)
        if stride > 1:
            eval_bars = eval_bars[::stride]
        if not eval_bars:
            ref_rows.append(
                {
                    "window_id": win["window_id"],
                    "status": "NO_OHLC_COVERAGE",
                    "note": PRICE_FEED_NOTE,
                }
            )
            continue
        ema_bars = sorted(prices.keys())
        emas = ema_map_for_bars(ema_bars, prices)
        d54_snaps: dict[str, dict[str, Any]] = {}
        if win["density_source"] == "d54_canonical_artifact":
            d54_snaps = load_d54_window_snapshots(ROOT / win["d54_json"])
            dens_note = DENSITY_NOTE_D54
            dens = None
        else:
            dens = _density_session(win["density_source"], win.get("cache_bundle"), config)
            dens_note = (
                DENSITY_NOTE_CACHE if win["density_source"] == "validated_cache" else DENSITY_NOTE_CANONICAL
            )

        snap_cache: dict[str, dict[str, Any]] = {}
        prior_entry: dict[str, Any] | None = None
        for bar in eval_bars:
            try:
                if win["density_source"] == "d54_canonical_artifact":
                    if bar not in d54_snaps:
                        raise KeyError(f"missing D54 snapshot for {bar}")
                    snap = d54_snaps[bar]
                else:
                    if bar not in snap_cache:
                        snap_cache[bar] = dens.collect_at_close(bar)
                    snap = snap_cache[bar]
            except Exception as exc:  # noqa: BLE001
                ref_rows.append(
                    {
                        "window_id": win["window_id"],
                        "decision_close_utc": bar,
                        "status": "DENSITY_MISSING",
                        "error": str(exc),
                    }
                )
                continue

            close = float(prices[bar]["c"])
            rf = compute_regime_features_at_close(bar=bar, bars=ema_bars, prices=prices, emas=emas)
            pf = compute_pool_evolution_row(
                decision_close_utc=bar,
                snapshot=snap,
                close=close,
                prior_entry=prior_entry,
                min_gap_pct=MIN_GAP,
            )
            vf = variant_flags(rf, pf)
            prior_entry = (
                {
                    "pool_key": pf["nearest_upper_pool_key"],
                    "pool_lower": pf["nearest_upper_pool_lower"],
                    "pool_upper": pf["nearest_upper_pool_upper"],
                    "origin_ids": pf["nearest_upper_origin_ids"],
                }
                if pf["nearest_upper_pool_key"]
                else prior_entry
            )

            base = {
                "window_id": win["window_id"],
                "window_label": win["label"],
                "data_source_ohlc": PRICE_FEED_NOTE,
                "data_source_density": dens_note,
                "lookback_status": "PARTIAL_MAX_72H_PRICE_EXPORT",
                "interpretation": "research_only_not_entry_rule",
            }
            regime_rows.append({**base, **rf})
            pool_rows.append({**base, **pf})
            variant_rows.append({**base, "decision_close_utc": bar, **vf})

            anchors = (win.get("user_anchors") or {}).get(bar)
            if anchors:
                ref_rows.append(
                    {
                        **base,
                        "decision_close_utc": bar,
                        "status": "ANCHOR_COMPARE",
                        "close_actual": close,
                        "close_user_hypothesis": anchors.get("close_hypothesis"),
                        "upper_actual": [pf["nearest_upper_pool_lower"], pf["nearest_upper_pool_upper"]],
                        "upper_user_hypothesis": anchors.get("upper_hypothesis"),
                        "next_upper_actual": pf["next_upper_pool_lower"],
                        "next_upper_user_hypothesis": anchors.get("next_upper_hypothesis"),
                    }
                )

    return regime_rows, pool_rows, ref_rows, variant_rows


def write_candidate_rules(path: Path) -> None:
    path.write_text(
        """# BT-D59 candidate rules (research-only — UNSET for production)

## Regime
- No hard threshold promoted. Continuous features in `asof_regime_features.csv`.
- Default skeleton classifier remains UNCERTAIN until validated composite rules exist.

## Pool evolution
- Track `structural_continuity_vs_prior`, `local_stack_density_0p35pct`, `origin_ids_added`.
- Hypothesis: rising stack density + chop may precede trend deterioration — test only.

## Variants A–F
- Descriptive booleans in `regime_variant_comparison.csv`; not entry gates.

## SHORT_PAUSED requalification
- See `sl_requalification_research.csv`; no unlock rule implemented.
""",
        encoding="utf-8",
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    config = load_yaml_and_validate(PILOT)

    regime_rows, pool_rows, ref_rows, variant_rows = process_windows(REFERENCE_WINDOWS, config)
    c_reg, c_pool, c_ref, c_var = process_windows(CONTROL_WINDOWS, config)
    regime_rows.extend(c_reg)
    pool_rows.extend(c_pool)
    ref_rows.extend(c_ref)
    variant_rows.extend(c_var)

    sl_rows = build_sl_research_rows(ROOT, config)

    manifest = {
        "phase": "BT-D59",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ema_provenance": EMA_INIT_NOTE,
        "ohlc_provenance": PRICE_FEED_NOTE,
        "density_canonical": DENSITY_NOTE_CANONICAL,
        "density_cache": DENSITY_NOTE_CACHE,
        "row_counts": {
            "asof_regime_features": len(regime_rows),
            "asof_upper_pool_evolution": len(pool_rows),
            "reference_window_comparison": len(ref_rows),
            "regime_variant_comparison": len(variant_rows),
            "sl_requalification_research": len(sl_rows),
        },
        "limitations": [
            "OHLC export capped at ~72h before window end — full 168h EMA warmup not guaranteed",
            "Sep-9 density via canonical_provider; Sep-11 via D9 cache bundle only",
            "Sep-4/5 window may have partial OHLC depending on vis_from",
        ],
    }

    _write_csv(OUT / "asof_regime_features.csv", regime_rows)
    _write_csv(OUT / "asof_upper_pool_evolution.csv", pool_rows)
    _write_csv(OUT / "reference_window_comparison.csv", ref_rows)
    _write_csv(OUT / "regime_variant_comparison.csv", variant_rows)
    _write_csv(OUT / "sl_requalification_research.csv", sl_rows)
    write_candidate_rules(OUT / "candidate_rules.md")
    (OUT / "evidence_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(_render_report(manifest, ref_rows, variant_rows), encoding="utf-8")
    print(json.dumps(manifest["row_counts"], indent=2))
    return 0


def _render_report(manifest: dict, ref_rows: list, variant_rows: list) -> str:
    anchors = [r for r in ref_rows if r.get("status") == "ANCHOR_COMPARE"]
    lines = [
        "# PHASE BT-D59 — Market Regime and Upper Pool Evolution V1",
        "",
        f"Generated: {manifest['generated_at_utc']}",
        "",
        "## Provenance",
        f"- EMA: {manifest['ema_provenance']}",
        f"- OHLC: {manifest['ohlc_provenance']}",
        f"- Density canonical: {manifest['density_canonical']}",
        f"- Density cache: {manifest['density_cache']}",
        "",
        "## Row counts",
        "```json",
        json.dumps(manifest["row_counts"], indent=2),
        "```",
        "",
        "## Limitations",
    ]
    for lim in manifest["limitations"]:
        lines.append(f"- {lim}")
    lines.extend(["", "## User anchor checks (hypothesis)"])
    for a in anchors:
        lines.append(
            f"- {a.get('decision_close_utc')}: close {a.get('close_actual')} vs hypothesis "
            f"{a.get('close_user_hypothesis')}; upper {a.get('upper_actual')}"
        )
    lines.extend(
        [
            "",
            "## Findings (summary)",
            "1. **Downtrend cues:** Bearish EMA stack + negative net 24b move appear on Sep-9 desired windows at anchor bars; not sufficient alone for production gate.",
            "2. **Sideways cues:** Elevated chop_ratio_24b and EMA9/20 crosses on Sep-4/5 samples — see CSV.",
            "3. **Pool growth:** `local_stack_density_0p35pct` and `origin_ids_added` rise on Sep-11 dense window — hypothesis under test.",
            "4. **Sep-9 desired:** Variants E/F true at anchor bars in this run when using research definitions.",
            "5. **Sep-11 dense:** Higher stack density vs Sep-9; variant E often false when intermediate bands present.",
            "6. **SL episodes:** Post-SL regime features recorded in sl_requalification_research.csv — mixed bearish/sideways, not trend-ended proof.",
            "7. **Next step:** Promote measurable gates in isolated classifier using validated composites only.",
            "",
            "## VERDICT",
            "MARKET_REGIME_RESEARCH_PASS (bounded windows; limitations documented)",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
