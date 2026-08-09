"""Export BE50 July audit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_be50_july_2026 import DEFINITIONS_DOC


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


def render_summary(p: dict[str, Any]) -> str:
    b, e = p["base_summary"], p["be_summary"]
    c = p["counts"]
    lines = [
        f"Primary Decision: **{p['decision']}**",
        "",
        f"- Price path: `{p['price_resolution']}`",
        f"- Fee model: gross − {p['fee_pct']}%",
        "",
        "## Headline",
        "",
        f"| Metric | Baseline | BE50 | Delta |",
        f"|---|---:|---:|---:|",
        f"| End Active | {b['end_active']:.2f} | {e['end_active']:.2f} | {e['end_active']-b['end_active']:+.2f} |",
        f"| End Reserve | {b['end_reserve']:.2f} | {e['end_reserve']:.2f} | {e['end_reserve']-b['end_reserve']:+.2f} |",
        f"| End Total | {b['end_total']:.2f} | {e['end_total']:.2f} | {e['end_total']-b['end_total']:+.2f} |",
        f"| Performance | {b['performance_pct']:+.2f}% | {e['performance_pct']:+.2f}% | {e['performance_pct']-b['performance_pct']:+.2f}pp |",
        f"| Max DD | {b['max_dd_pct']:.2f}% | {e['max_dd_pct']:.2f}% | {e['max_dd_pct']-b['max_dd_pct']:+.2f}pp |",
        f"| TP | {b['n_tp']} | {e['n_tp']} | {e['n_tp']-b['n_tp']:+d} |",
        f"| SL | {b['n_sl']} | {e['n_sl']} | {e['n_sl']-b['n_sl']:+d} |",
        f"| BE | 0 | {e['n_be']} | {e['n_be']:+d} |",
        f"| Longest SL streak | {b['longest_sl_streak']} | {e['longest_sl_streak']} | {e['longest_sl_streak']-b['longest_sl_streak']:+d} |",
        f"| Longest non-winner streak | {b['longest_nonwinner_streak']} | {e['longest_nonwinner_streak']} | {e['longest_nonwinner_streak']-b['longest_nonwinner_streak']:+d} |",
        "",
        f"- SL→BE: **{c['SL_TO_BE']}**",
        f"- TP→BE: **{c['TP_TO_BE']}**",
        f"- Unchanged TP/SL: {c['UNCHANGED_TP']} / {c['UNCHANGED_SL']}",
        f"- Ambiguous intrabar: {c['n_ambiguous']}",
        f"- Saved loss (sum Δnet on SL→BE): **{p['total_saved_loss_pct']:+.2f}pp**",
        f"- Lost winner profit (sum Δnet on TP→BE): **{p['total_lost_winner_profit_pct']:+.2f}pp**",
        f"- BE50_net_benefit (pct-sum): **{p['be50_net_benefit_pct']:+.2f}pp**",
        f"- Equity delta: **{p['equity_delta']:+.2f} USDT**",
        "",
        "## Did BE50 save more bad trades than it destroyed good ones?",
        "",
        (
            f"Yes — SL→BE ({c['SL_TO_BE']}) > TP→BE ({c['TP_TO_BE']}) and equity rose."
            if c["SL_TO_BE"] > c["TP_TO_BE"] and p["equity_delta"] > 0
            else (
                f"Mixed/No — SL→BE={c['SL_TO_BE']}, TP→BE={c['TP_TO_BE']}, "
                f"equity Δ={p['equity_delta']:+.2f}."
            )
        ),
        "",
        "## Cluster #9–13",
        "",
        "| Trade | Baseline | BE50 | Trigger? | Delta net |",
        "|---|---|---|---|---:|",
    ]
    for _, r in p["cluster_9_13"].iterrows():
        lines.append(
            f"| #{int(r['july_n'])} | {r['baseline_reason']} {r['baseline_net_pct']:+.2f}% | "
            f"{r['be50_reason']} {r['be50_net_pct']:+.2f}% | {r['be50_triggered']} | "
            f"{r['pnl_delta_pct']:+.2f} |"
        )
    lines += ["", "## Groups", ""]
    g = p["groups"]
    lines.append("| Group | Baseline Net | BE50 Net | SL→BE | TP→BE | Delta |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for _, r in g.iterrows():
        lines.append(
            f"| {r['group_type']}={r['group']} | {r['baseline_net_sum']:+.2f} | "
            f"{r['be50_net_sum']:+.2f} | {r['SL_TO_BE']} | {r['TP_TO_BE']} | {r['delta_net_sum']:+.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    cmp = payload["comparison"]

    def w(name, df):
        p = out_dir / name
        df.to_csv(p, index=False)
        paths[name] = p

    w("be50_trade_comparison.csv", cmp)
    w("be50_equity_curve.csv", payload["be_equity"])
    w("baseline_equity_curve.csv", payload["base_equity"])
    changed = cmp[cmp["outcome_changed"]].copy()
    w("be50_changed_trades.csv", changed)
    w("be50_sl_to_be.csv", payload["sl_to_be_df"])
    w("be50_tp_to_be.csv", payload["tp_to_be_df"])
    w("be50_group_comparison.csv", payload["groups"])
    w("be50_cluster_9_13.csv", payload["cluster_9_13"])

    p = out_dir / "DEFINITIONS.md"
    p.write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["definitions"] = p

    p = out_dir / "summary.md"
    p.write_text(render_summary(payload), encoding="utf-8")
    paths["summary_md"] = p

    summary = {
        "audit_version": payload["audit_version"],
        "decision": payload["decision"],
        "price_resolution": payload["price_resolution"],
        "fee_pct": payload["fee_pct"],
        "n_trades": payload["n_trades"],
        "baseline": payload["base_summary"],
        "be50": payload["be_summary"],
        "counts": payload["counts"],
        "total_saved_loss_pct": payload["total_saved_loss_pct"],
        "total_lost_winner_profit_pct": payload["total_lost_winner_profit_pct"],
        "be50_net_benefit_pct": payload["be50_net_benefit_pct"],
        "equity_delta": payload["equity_delta"],
        "sl_to_be_n": payload["sl_to_be_n"],
    }
    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(summary), indent=2) + "\n", encoding="utf-8")
    paths["summary_json"] = p
    return paths
