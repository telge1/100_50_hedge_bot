"""Side-by-side feature replay for drift forensics (read-only)."""

from __future__ import annotations

import copy
import csv
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

REFERENCE_TS = [
    "2026-09-09T16:30:00Z",
    "2026-09-09T20:55:00Z",
    "2026-09-11T12:00:00Z",
    "2026-09-11T12:05:00Z",
    "2026-09-11T12:30:00Z",
]

for p in (
    ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
    ROOT / "tools/phase_bt_d62_intent_gate_replay_v1",
    ROOT / "tools/phase_bt_d63_extended_timestamp_validation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ablation_v1 import ablation_flags  # noqa: E402
from gate_policies_v1 import evaluate_policies  # noqa: E402
from regime_labels_v1 import ema59_flattening, research_regime  # noqa: E402


def _load_d61(ts: str) -> dict[str, Any]:
    path = ROOT / "results/phase_bt_d61_regime_audit_v1/per_close_regime_comparison.csv"
    for r in csv.DictReader(path.open(encoding="utf-8")):
        if r["decision_close_utc"] == ts:
            return dict(r)
    return {}


def _load_d63(ts: str) -> dict[str, Any]:
    path = ROOT / "results/phase_bt_d63_extended_timestamps_v1/causal_gate_decisions.csv"
    for r in csv.DictReader(path.open(encoding="utf-8")):
        if r["decision_close_utc"] == ts:
            return dict(r)
    return {}


def _load_d62(ts: str) -> dict[str, Any]:
    path = ROOT / "results/phase_bt_d62_intent_gate_replay_v1/gate_decisions_at_intent.csv"
    for r in csv.DictReader(path.open(encoding="utf-8")):
        if r["decision_close_utc"] == ts:
            return dict(r)
    return {}


def _bool(v: Any) -> bool:
    return v in (True, "True", "true", "1", 1)


def _r6_components(row: dict[str, Any]) -> dict[str, Any]:
    pool_ok = row.get("pool_validity") in ("OK", "OK_D59_CACHE")
    chop = float(row.get("chop_ratio_24b") or 0)
    net = float(row.get("net_move_24b_pct") or 0)
    stack = int(float(row.get("local_stack_density_0p35pct") or 0))
    ema200_12 = row.get("ema200_slope_12b_pct")
    try:
        ema200_12f = float(ema200_12) if ema200_12 not in (None, "") else None
    except ValueError:
        ema200_12f = None
    flat = ema59_flattening(row)
    return {
        "score_chop_ge_6": chop >= 6.0,
        "score_net_abs_lt_0_35": abs(net) < 0.35,
        "score_flat59_fall200": flat and ema200_12f is not None and ema200_12f < 0,
        "score_stack_ge_3": pool_ok and stack >= 3,
        "score_origin_added": pool_ok and _bool(row.get("origin_ids_added")),
        "sideways_score": sum(
            [
                chop >= 6.0,
                abs(net) < 0.35,
                flat and ema200_12f is not None and ema200_12f < 0,
                pool_ok and stack >= 3,
                pool_ok and _bool(row.get("origin_ids_added")),
            ]
        ),
    }


def replay_d63_no_prior(ts: str, config: dict) -> dict[str, Any]:
    """D63 pool attach with prior_entry=None (parity fix candidate)."""
    from d54_snapshot_loader_v1 import load_d54_window_snapshots  # noqa: E402
    from evaluate_close_v1 import load_ema_bundle  # noqa: E402
    from pool_evolution_v1 import compute_pool_evolution_row  # noqa: E402
    from regime_features_v1 import compute_regime_features_at_close  # noqa: E402
    from enrich_v1 import attach_ema_slopes  # noqa: E402

    g_start, g_end = "2026-09-09T16:00:00Z", "2026-09-09T22:00:00Z"
    bars_regime, prices, emas = load_ema_bundle(g_start, g_end)
    if ts not in prices:
        return {"error": "no_price"}
    feats = compute_regime_features_at_close(bar=ts, bars=bars_regime, prices=prices, emas=emas)
    row = {**feats, "decision_close_utc": ts, "ema_validity": "VALIDATED_TRP_D60"}
    attach_ema_slopes(row, bars_regime, emas)
    wjson = ROOT / "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window2_2026_09_09_2055_2125.json"
    if ts < "2026-09-09T20:55:00Z":
        wjson = ROOT / "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window1_2026_09_09_1630_1725.json"
    snaps = load_d54_window_snapshots(wjson)
    snap = snaps[ts]
    pf = compute_pool_evolution_row(
        decision_close_utc=ts,
        snapshot=snap,
        close=float(row["close"]),
        prior_entry=None,
        min_gap_pct=0.5,
    )
    row.update(pf)
    row["pool_validity"] = "OK"
    flags = ablation_flags(row)
    row["R5"] = flags["variant_E_plus_no_stack_growth"]
    row["R6_sideways"] = research_regime(row) == "SIDEWAYS_CANDIDATE"
    row["research_regime"] = research_regime(row)
    pol = evaluate_policies({**row, "R5": row["R5"], "R6_sideways": row["R6_sideways"]}, short_paused=False)
    row["P2"] = pol["policy_p2_r6_veto"]
    row["path"] = "d63_pool_prior_none"
    return row


def field_diff(ts: str, config: dict) -> list[dict[str, Any]]:
    d61 = _load_d61(ts)
    d62 = _load_d62(ts)
    d63 = _load_d63(ts)
    fixed = replay_d63_no_prior(ts, config) if ts.startswith("2026-09-09") else {}
    fields = [
        "close",
        "ema9",
        "ema20",
        "ema59",
        "ema200",
        "chop_ratio_24b",
        "net_move_24b_pct",
        "origin_ids_added",
        "structural_continuity_vs_prior",
        "local_stack_density_0p35pct",
        "gap_to_next_upper_pct",
        "research_regime",
        "R5",
        "R6_sideways",
        "P2",
    ]
    rows: list[dict[str, Any]] = []
    for f in fields:
        rows.append(
            {
                "decision_close_utc": ts,
                "field": f,
                "d61_cached": d61.get(f),
                "d62_gate": d62.get(f) or d62.get("policy_p2_r6_veto") if f == "P2" else d62.get(f),
                "d63_extended": d63.get(f),
                "d63_prior_none_replay": fixed.get(f),
            }
        )
    comp = _r6_components(d61 if d61 else d63)
    for k, v in comp.items():
        rows.append(
            {
                "decision_close_utc": ts,
                "field": f"r6_{k}",
                "d61_cached": v if d61 else "",
                "d63_extended": _r6_components(d63).get(k) if d63 else "",
            }
        )
    return rows


def compare_all(config: dict) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_diffs: list[dict[str, Any]] = []
    first_div: dict[str, Any] = {}
    for ts in REFERENCE_TS:
        for row in field_diff(ts, config):
            all_diffs.append(row)
            if ts == "2026-09-09T20:55:00Z" and row["field"] == "origin_ids_added":
                if row.get("d61_cached") != row.get("d63_extended"):
                    first_div = {
                        "timestamp": ts,
                        "first_diverging_field": "origin_ids_added",
                        "d61": row.get("d61_cached"),
                        "d63": row.get("d63_extended"),
                        "cause": "D63 pool_evolution prior_entry chained across 16:00-22:00 window; D61/D62 use per-reference-window prior (NO_PRIOR at 20:55)",
                    }
    return all_diffs, first_div
