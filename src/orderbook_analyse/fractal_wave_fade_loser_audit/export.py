"""Export loser audit artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_loser_audit import DEFINITIONS_DOC


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        t = x.tz_convert("UTC") if x.tzinfo else x.tz_localize("UTC")
        return t.strftime("%Y-%m-%d %H:%M:%S UTC")
    if hasattr(x, "item"):
        try:
            return x.item()
        except Exception:
            pass
    if isinstance(x, float) and (x != x):
        return None
    return x


def render_summary_md(p: dict[str, Any]) -> str:
    d = p["decision"]
    a = p["answers"]
    lines = [
        f"Primary Decision: **{d['decision']}**",
        "",
        "## 3 key findings",
        "",
    ]
    # top findings from data
    mfe = p["mfe_reach"]
    lines.append(
        f"1. **MFE before SL:** {mfe['immediate_failure']}/30 IMMEDIATE_FAILURE, "
        f"{mfe['partial']} PARTIAL, {mfe['near_tp']} NEAR_TP — "
        f"≥50% of TP reached as MFE in only {mfe['mfe_ge_50pct_tp']}/30 cases."
    )
    if p["top5"]:
        t0 = p["top5"][0]
        lines.append(
            f"2. **Strongest pattern:** `{t0['name']}` — SL {t0['sl_affected']}, "
            f"winners {t0['winners_affected']}, filter candidate: **{t0['filter_candidate']}**."
        )
    lines.append(
        f"3. **Dominant failure mode:** `{d['dominant_failure_mode']}` "
        f"(filterable candidates: {d.get('filterable_candidates')})."
    )
    lines += ["", "## Answers", ""]
    lines.append(f"1. Häufigstes Verlustmuster: `{a['q1_most_common_loss_pattern']}`")
    lines.append(f"2. SLs betroffen: {a['q2_sls_affected']}")
    lines.append(f"3. Dasselbe bei Gewinnern: {a['q3_same_in_winners']}")
    lines.append(f"4. Signaltyp mit meisten SLs: `{a['q4_signal_type_most_sls']}` ({a['q4_count']})")
    lines.append(f"5. SHORT vs LONG: {a['q5_short_vs_long']}")
    lines.append(f"6. DOGE vs APT: {a['q6_doge_vs_apt']}")
    lines.append(f"7. Serie #9–13: {a['q7_cluster_9_13']}")
    lines.append(f"8. IMMEDIATE_FAILURE: {a['q8_immediate_failures']}")
    lines.append(f"9. MFE thresholds: {a['q9_mfe_thresholds']}")
    lines.append(f"10. Einfacher Filterkandidat: {a['q10_simple_causal_filter']}")
    lines += ["", "## Top patterns", ""]
    for i, t in enumerate(p["top5"], 1):
        lines.append(f"### Pattern {i}: `{t['name']}`")
        lines.append(f"- SLs: {t['sl_affected']}")
        lines.append(f"- Winners: {t['winners_affected']}")
        lines.append(f"- SL-rate with/without: {t['sl_rate_with']} / {t['sl_rate_without']}")
        lines.append(f"- Lift: {t['lift']}")
        lines.append(f"- Filterkandidat: **{t['filter_candidate']}**")
        lines.append("")
    lines += ["", "## SL clusters", ""]
    for c in p["cluster_notes"]:
        lines.append(f"- **{c['focus']}** n={c['n']}: sides={c['sides']} symbols={c['symbols']} "
                     f"htf={c['htf']} mfe={c['mfe_class']} mode={c['failure_mode']} repeated={c['repeated_fade']}")
    lines.append("")
    lines.append("No strategy change. Patterns are diagnostic only.")
    lines.append("")
    return "\n".join(lines)


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    def w(name, df):
        p = out_dir / name
        df.to_csv(p, index=False)
        paths[name] = p

    losers = payload["losers"]
    winners = payload["winners"]

    # core loser trades compact
    trade_cols = [
        c for c in [
            "july_n", "trade_id", "symbol", "side", "entry_time", "entry_price",
            "final_tp_price", "final_sl_price", "exit_time", "exit_price",
            "net_return_pct", "first_signal_tf", "signal_type", "wave_direction",
            "tpsl_profile", "failure_mode", "mfe_class",
        ] if c in losers.columns
    ]
    w("loser_trades.csv", losers[trade_cols])
    w("loser_diagnostics.csv", losers)
    w("loser_signal_types.csv", payload["signal_types"])
    w("loser_failure_modes.csv", payload["failure_modes"])
    w(
        "loser_mfe_before_sl.csv",
        losers[
            [
                c
                for c in [
                    "july_n", "trade_id", "symbol", "side", "final_tp_pct",
                    "mfe_pct", "mae_pct", "mfe_to_tp", "mfe_class",
                    "immediate_adverse", "bars_to_sl",
                ]
                if c in losers.columns
            ]
        ],
    )
    w("loser_clusters.csv", payload["clusters"])
    w("candidate_patterns.csv", payload["patterns"])
    # winner control comparison = patterns table already compares
    w("winner_control_comparison.csv", payload["patterns"])
    # also dump winners diagnostics for transparency
    w("winner_diagnostics.csv", winners)

    p = out_dir / "DEFINITIONS.md"
    p.write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["definitions"] = p

    p = out_dir / "summary.md"
    p.write_text(render_summary_md(payload), encoding="utf-8")
    paths["summary_md"] = p

    summary = {
        "audit_version": payload["audit_version"],
        "n_sl": payload["n_sl"],
        "n_tp": payload["n_tp"],
        "decision": payload["decision"],
        "answers": payload["answers"],
        "top5": payload["top5"],
        "mfe_reach": payload["mfe_reach"],
        "cluster_notes": payload["cluster_notes"],
        "failure_mode_counts": payload["failure_modes"].set_index("failure_mode")["n"].to_dict()
        if len(payload["failure_modes"])
        else {},
    }
    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(summary), indent=2) + "\n", encoding="utf-8")
    paths["summary_json"] = p
    return paths
